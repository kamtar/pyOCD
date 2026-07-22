// pyOCD debugger
// Copyright (c) 2026 Arm Limited
// SPDX-License-Identifier: Apache-2.0

#define _POSIX_C_SOURCE 200809L
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define MAP_SIZE 4096
#define PIN_COUNT 54
#define GPFSEL0 0
#define GPSET0 7
#define GPCLR0 10
#define GPLEV0 13
#define MODE_INPUT 0
#define MODE_OUTPUT 1
#define MAX_SEQUENCE_BITS 4096
#define MAX_TRANSACTION_WORDS (1024 * 1024)
#define LOOP_CALIBRATION_ITERATIONS 1000000U
#define GPIO_CALIBRATION_ITERATIONS 256U

typedef struct {
    PyObject_HEAD
    int fd;
    volatile uint32_t *registers;
    uint64_t half_period_ns;
    double delay_loops_per_ns;
} BCMEngine;

typedef struct {
    bool is_read;
    unsigned int count;
    uint8_t *data;
} swd_operation_t;

typedef struct {
    volatile uint32_t *swclk_set;
    volatile uint32_t *swclk_clear;
    volatile uint32_t *swdio_set;
    volatile uint32_t *swdio_clear;
    volatile uint32_t *swdio_level;
    volatile uint32_t *swdio_mode;
    uint32_t swclk_mask;
    uint32_t swdio_mask;
    uint32_t swdio_mode_clear_mask;
    uint32_t swdio_mode_output_bits;
    int swdio_dir;
    volatile uint32_t *swdio_dir_set;
    volatile uint32_t *swdio_dir_clear;
    uint32_t swdio_dir_mask;
    int direction_is_output;
    uint32_t delay_iterations;
} swd_bus_t;

static PyObject *SWDError;
static PyObject *SWDWaitError;
static PyObject *SWDFaultError;
static PyObject *SWDProtocolError;

static bool validate_pin(unsigned int pin);

static inline void gpio_sync(void)
{
    __sync_synchronize();
}

static inline void gpio_write(BCMEngine *self, unsigned int pin, bool value)
{
    unsigned int index = (value ? GPSET0 : GPCLR0) + pin / 32;
    self->registers[index] = (uint32_t)1 << (pin % 32);
}

static inline bool gpio_read(BCMEngine *self, unsigned int pin)
{
    return (self->registers[GPLEV0 + pin / 32] & ((uint32_t)1 << (pin % 32))) != 0;
}

static inline void gpio_set_mode(BCMEngine *self, unsigned int pin, unsigned int mode)
{
    unsigned int index = GPFSEL0 + pin / 10;
    unsigned int shift = (pin % 10) * 3;
    uint32_t value = self->registers[index];
    self->registers[index] = (value & ~((uint32_t)0x7 << shift)) | ((uint32_t)mode << shift);
    gpio_sync();
}

static inline uint64_t monotonic_ns(void)
{
    struct timespec timestamp;
#ifdef CLOCK_MONOTONIC_RAW
    clock_gettime(CLOCK_MONOTONIC_RAW, &timestamp);
#else
    clock_gettime(CLOCK_MONOTONIC, &timestamp);
#endif
    return (uint64_t)timestamp.tv_sec * 1000000000ULL + (uint64_t)timestamp.tv_nsec;
}

/* This deliberately simple loop is calibrated after gpiomem is mapped. The
 * volatile counter keeps the compiler from replacing or deleting the loop,
 * while avoiding a clock syscall on every SWCLK edge. */
static inline void delay_loop(uint32_t iterations)
{
    volatile uint32_t remaining = iterations;
    while (remaining != 0)
        --remaining;
}

static double calibrate_delay_loop(void)
{
    uint64_t start = monotonic_ns();
    delay_loop(LOOP_CALIBRATION_ITERATIONS);
    uint64_t elapsed = monotonic_ns() - start;
    return elapsed ? (double)LOOP_CALIBRATION_ITERATIONS / (double)elapsed : 0.0;
}

static inline void wait_for_edge(swd_bus_t *bus)
{
    /* Explicit fast path for clocks at or above the attainable GPIO rate. */
    if (bus->delay_iterations != 0)
        delay_loop(bus->delay_iterations);
}

static bool init_swd_bus(
    BCMEngine *self,
    unsigned int swclk,
    unsigned int swdio,
    int swdio_dir,
    swd_bus_t *bus)
{
    if (!validate_pin(swclk) || !validate_pin(swdio))
        return false;
    if (swdio_dir >= 0 && !validate_pin((unsigned int)swdio_dir))
        return false;

    memset(bus, 0, sizeof(*bus));
    bus->swclk_set = &self->registers[GPSET0 + swclk / 32];
    bus->swclk_clear = &self->registers[GPCLR0 + swclk / 32];
    bus->swdio_set = &self->registers[GPSET0 + swdio / 32];
    bus->swdio_clear = &self->registers[GPCLR0 + swdio / 32];
    bus->swdio_level = &self->registers[GPLEV0 + swdio / 32];
    bus->swdio_mode = &self->registers[GPFSEL0 + swdio / 10];
    bus->swclk_mask = (uint32_t)1 << (swclk % 32);
    bus->swdio_mask = (uint32_t)1 << (swdio % 32);
    unsigned int mode_shift = (swdio % 10) * 3;
    bus->swdio_mode_clear_mask = ~((uint32_t)0x7 << mode_shift);
    bus->swdio_mode_output_bits = (uint32_t)MODE_OUTPUT << mode_shift;
    bus->swdio_dir = swdio_dir;
    if (swdio_dir >= 0) {
        bus->swdio_dir_set = &self->registers[GPSET0 + (unsigned int)swdio_dir / 32];
        bus->swdio_dir_clear = &self->registers[GPCLR0 + (unsigned int)swdio_dir / 32];
        bus->swdio_dir_mask = (uint32_t)1 << ((unsigned int)swdio_dir % 32);
    }
    bus->direction_is_output = -1;

    /* Measure the cost already paid by each edge (one MMIO write plus the
     * ordering barrier). Rewriting the high latch is electrically harmless.
     * Subtracting this cost makes the selected frequency describe the complete
     * edge period rather than an extra delay added after GPIO access. */
    uint64_t start = monotonic_ns();
    for (unsigned int index = 0; index < GPIO_CALIBRATION_ITERATIONS; ++index) {
        *bus->swclk_set = bus->swclk_mask;
        gpio_sync();
    }
    uint64_t elapsed = monotonic_ns() - start;
    double edge_overhead_ns = (double)elapsed / GPIO_CALIBRATION_ITERATIONS;
    double requested_delay_ns = (double)self->half_period_ns - edge_overhead_ns;
    double delay_iterations = requested_delay_ns * self->delay_loops_per_ns;
    if (requested_delay_ns <= 0.0 || delay_iterations < 1.0) {
        bus->delay_iterations = 0;
    } else if (delay_iterations > UINT32_MAX) {
        bus->delay_iterations = UINT32_MAX;
    } else {
        bus->delay_iterations = (uint32_t)(delay_iterations + 0.5);
    }
    return true;
}

static inline void set_swdio_direction(swd_bus_t *bus, bool output, bool first_bit)
{
    if (bus->direction_is_output == (int)output)
        return;
    if (output) {
        *(first_bit ? bus->swdio_set : bus->swdio_clear) = bus->swdio_mask;
        *bus->swdio_mode = (*bus->swdio_mode & bus->swdio_mode_clear_mask)
            | bus->swdio_mode_output_bits;
        if (bus->swdio_dir >= 0)
            *bus->swdio_dir_set = bus->swdio_dir_mask;
    } else {
        *bus->swdio_mode &= bus->swdio_mode_clear_mask;
        if (bus->swdio_dir >= 0)
            *bus->swdio_dir_clear = bus->swdio_dir_mask;
    }
    gpio_sync();
    bus->direction_is_output = output;
}

static inline void clock_write_bit(swd_bus_t *bus, bool bit)
{
    *bus->swclk_clear = bus->swclk_mask;
    *(bit ? bus->swdio_set : bus->swdio_clear) = bus->swdio_mask;
    gpio_sync();
    wait_for_edge(bus);
    *bus->swclk_set = bus->swclk_mask;
    gpio_sync();
    wait_for_edge(bus);
}

static inline bool clock_read_bit(swd_bus_t *bus)
{
    *bus->swclk_clear = bus->swclk_mask;
    gpio_sync();
    wait_for_edge(bus);
    bool value = (*bus->swdio_level & bus->swdio_mask) != 0;
    *bus->swclk_set = bus->swclk_mask;
    gpio_sync();
    wait_for_edge(bus);
    return value;
}

static void write_bits(swd_bus_t *bus, const uint8_t *data, unsigned int count)
{
    bool first_bit = count && (data[0] & 1);
    set_swdio_direction(bus, true, first_bit);
    for (unsigned int bit = 0; bit < count; ++bit)
        clock_write_bit(bus, (data[bit / 8] & (1U << (bit % 8))) != 0);
}

static uint64_t read_bits(swd_bus_t *bus, unsigned int count)
{
    uint64_t value = 0;
    set_swdio_direction(bus, false, false);
    for (unsigned int bit = 0; bit < count; ++bit) {
        if (clock_read_bit(bus))
            value |= (uint64_t)1 << bit;
    }
    return value;
}

static bool validate_pin(unsigned int pin)
{
    if (pin >= PIN_COUNT) {
        PyErr_Format(PyExc_ValueError, "GPIO number must be between 0 and %d", PIN_COUNT - 1);
        return false;
    }
    return true;
}

static bool require_open(BCMEngine *self)
{
    if (self->registers == MAP_FAILED) {
        PyErr_SetString(PyExc_RuntimeError, "Raspberry Pi GPIO engine is not open");
        return false;
    }
    return true;
}

static void engine_close(BCMEngine *self)
{
    if (self->registers != MAP_FAILED) {
        munmap((void *)self->registers, MAP_SIZE);
        self->registers = MAP_FAILED;
    }
    if (self->fd >= 0) {
        close(self->fd);
        self->fd = -1;
    }
}

static PyObject *BCMEngine_new(
    PyTypeObject *type,
    PyObject *Py_UNUSED(args),
    PyObject *Py_UNUSED(kwargs))
{
    BCMEngine *self = (BCMEngine *)type->tp_alloc(type, 0);
    if (self) {
        self->fd = -1;
        self->registers = MAP_FAILED;
        self->half_period_ns = 500;
        self->delay_loops_per_ns = 0.0;
    }
    return (PyObject *)self;
}

static void BCMEngine_dealloc(BCMEngine *self)
{
    engine_close(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *BCMEngine_open(BCMEngine *self, PyObject *args)
{
    const char *device;
    if (!PyArg_ParseTuple(args, "s:open", &device))
        return NULL;
    if (self->registers != MAP_FAILED)
        Py_RETURN_NONE;

    self->fd = open(device, O_RDWR | O_SYNC);
    if (self->fd < 0)
        return PyErr_SetFromErrnoWithFilename(PyExc_OSError, device);

    self->registers = mmap(NULL, MAP_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, self->fd, 0);
    if (self->registers == MAP_FAILED) {
        int saved_errno = errno;
        close(self->fd);
        self->fd = -1;
        errno = saved_errno;
        return PyErr_SetFromErrnoWithFilename(PyExc_OSError, device);
    }
    self->delay_loops_per_ns = calibrate_delay_loop();
    Py_RETURN_NONE;
}

static PyObject *BCMEngine_close(BCMEngine *self, PyObject *Py_UNUSED(ignored))
{
    engine_close(self);
    Py_RETURN_NONE;
}

static PyObject *BCMEngine_get_is_open(BCMEngine *self, void *Py_UNUSED(closure))
{
    return PyBool_FromLong(self->registers != MAP_FAILED);
}

static PyObject *BCMEngine_set_frequency(BCMEngine *self, PyObject *args)
{
    double frequency;
    if (!PyArg_ParseTuple(args, "d:set_frequency", &frequency))
        return NULL;
    if (frequency <= 0) {
        PyErr_SetString(PyExc_ValueError, "SWD frequency must be greater than zero");
        return NULL;
    }
    self->half_period_ns = (uint64_t)(500000000.0 / frequency);
    Py_RETURN_NONE;
}

static PyObject *BCMEngine_get_mode(BCMEngine *self, PyObject *args)
{
    unsigned int pin;
    if (!PyArg_ParseTuple(args, "I:get_mode", &pin) || !require_open(self) || !validate_pin(pin))
        return NULL;
    unsigned int shift = (pin % 10) * 3;
    return PyLong_FromUnsignedLong((self->registers[GPFSEL0 + pin / 10] >> shift) & 0x7);
}

static PyObject *BCMEngine_set_mode(BCMEngine *self, PyObject *args)
{
    unsigned int pin;
    unsigned int mode;
    if (!PyArg_ParseTuple(args, "II:set_mode", &pin, &mode) || !require_open(self) || !validate_pin(pin))
        return NULL;
    if (mode > 7) {
        PyErr_SetString(PyExc_ValueError, "GPIO function must be between 0 and 7");
        return NULL;
    }
    gpio_set_mode(self, pin, mode);
    Py_RETURN_NONE;
}

static PyObject *BCMEngine_write(BCMEngine *self, PyObject *args)
{
    unsigned int pin;
    int value;
    if (!PyArg_ParseTuple(args, "Ip:write", &pin, &value) || !require_open(self) || !validate_pin(pin))
        return NULL;
    gpio_write(self, pin, value != 0);
    gpio_sync();
    Py_RETURN_NONE;
}

static PyObject *BCMEngine_read(BCMEngine *self, PyObject *args)
{
    unsigned int pin;
    if (!PyArg_ParseTuple(args, "I:read", &pin) || !require_open(self) || !validate_pin(pin))
        return NULL;
    return PyBool_FromLong(gpio_read(self, pin));
}

static void free_operations(swd_operation_t *operations, Py_ssize_t operation_count)
{
    if (!operations)
        return;
    for (Py_ssize_t index = 0; index < operation_count; ++index)
        free(operations[index].data);
    free(operations);
}

static bool parse_operations(PyObject *sequence_object, swd_operation_t **result, Py_ssize_t *result_count)
{
    PyObject *fast_sequences = PySequence_Fast(sequence_object, "sequences must be iterable");
    if (!fast_sequences)
        return false;

    Py_ssize_t count = PySequence_Fast_GET_SIZE(fast_sequences);
    swd_operation_t *operations = calloc((size_t)count, sizeof(*operations));
    if (!operations) {
        Py_DECREF(fast_sequences);
        PyErr_NoMemory();
        return false;
    }

    for (Py_ssize_t index = 0; index < count; ++index) {
        PyObject *item = PySequence_Fast(PySequence_Fast_GET_ITEM(fast_sequences, index),
            "each SWD sequence must be iterable");
        if (!item)
            goto error;
        Py_ssize_t item_count = PySequence_Fast_GET_SIZE(item);
        if (item_count != 1 && item_count != 2) {
            Py_DECREF(item);
            PyErr_SetString(PyExc_ValueError, "SWD sequence entries must contain one or two values");
            goto error;
        }

        unsigned long bit_count = PyLong_AsUnsignedLong(PySequence_Fast_GET_ITEM(item, 0));
        if (PyErr_Occurred() || bit_count > MAX_SEQUENCE_BITS) {
            Py_DECREF(item);
            if (!PyErr_Occurred())
                PyErr_Format(PyExc_ValueError, "SWD sequence cannot exceed %d bits", MAX_SEQUENCE_BITS);
            goto error;
        }
        operations[index].count = (unsigned int)bit_count;
        operations[index].is_read = item_count == 1;

        size_t byte_count = ((size_t)bit_count + 7) / 8;
        operations[index].data = calloc(byte_count ? byte_count : 1, 1);
        if (!operations[index].data) {
            Py_DECREF(item);
            PyErr_NoMemory();
            goto error;
        }

        if (!operations[index].is_read) {
            PyObject *buffer_object = PySequence_Fast_GET_ITEM(item, 1);
            Py_buffer buffer;
            if (PyObject_GetBuffer(buffer_object, &buffer, PyBUF_SIMPLE) < 0) {
                Py_DECREF(item);
                goto error;
            }
            if ((size_t)buffer.len < byte_count) {
                PyBuffer_Release(&buffer);
                Py_DECREF(item);
                PyErr_SetString(PyExc_ValueError, "SWD output data is shorter than its bit count");
                goto error;
            }
            memcpy(operations[index].data, buffer.buf, byte_count);
            PyBuffer_Release(&buffer);
        }
        Py_DECREF(item);
    }

    Py_DECREF(fast_sequences);
    *result = operations;
    *result_count = count;
    return true;

error:
    Py_DECREF(fast_sequences);
    free_operations(operations, count);
    return false;
}

static void execute_operations(
    BCMEngine *self,
    unsigned int swclk,
    unsigned int swdio,
    int swdio_dir,
    swd_operation_t *operations,
    Py_ssize_t operation_count)
{
    swd_bus_t bus;
    if (!init_swd_bus(self, swclk, swdio, swdio_dir, &bus))
        return;
    for (Py_ssize_t operation_index = 0; operation_index < operation_count; ++operation_index) {
        swd_operation_t *operation = &operations[operation_index];
        bool first_bit = operation->count && (operation->data[0] & 1);
        set_swdio_direction(&bus, !operation->is_read, first_bit);
        for (unsigned int bit_index = 0; bit_index < operation->count; ++bit_index) {
            if (!operation->is_read) {
                bool bit = (operation->data[bit_index / 8] & (1U << (bit_index % 8))) != 0;
                clock_write_bit(&bus, bit);
            } else if (clock_read_bit(&bus)) {
                operation->data[bit_index / 8] |= (uint8_t)(1U << (bit_index % 8));
            }
        }
    }
}

static PyObject *BCMEngine_transfer(BCMEngine *self, PyObject *args)
{
    unsigned int swclk;
    unsigned int swdio;
    int swdio_dir;
    PyObject *sequence_object;
    if (!PyArg_ParseTuple(args, "IIiO:transfer", &swclk, &swdio, &swdio_dir, &sequence_object)
        || !require_open(self) || !validate_pin(swclk) || !validate_pin(swdio))
        return NULL;
    if (swdio_dir >= 0 && !validate_pin((unsigned int)swdio_dir))
        return NULL;

    swd_operation_t *operations = NULL;
    Py_ssize_t operation_count = 0;
    if (!parse_operations(sequence_object, &operations, &operation_count))
        return NULL;

    Py_BEGIN_ALLOW_THREADS
    execute_operations(self, swclk, swdio, swdio_dir, operations, operation_count);
    Py_END_ALLOW_THREADS

    PyObject *reads = PyList_New(0);
    if (!reads) {
        free_operations(operations, operation_count);
        return NULL;
    }
    for (Py_ssize_t index = 0; index < operation_count; ++index) {
        if (!operations[index].is_read)
            continue;
        Py_ssize_t byte_count = (Py_ssize_t)((operations[index].count + 7) / 8);
        PyObject *value = PyBytes_FromStringAndSize((const char *)operations[index].data, byte_count);
        if (!value || PyList_Append(reads, value) < 0) {
            Py_XDECREF(value);
            Py_DECREF(reads);
            free_operations(operations, operation_count);
            return NULL;
        }
        Py_DECREF(value);
    }
    free_operations(operations, operation_count);
    return reads;
}

enum swd_status {
    SWD_STATUS_OK,
    SWD_STATUS_WAIT,
    SWD_STATUS_FAULT,
    SWD_STATUS_PROTOCOL,
    SWD_STATUS_PARITY,
};

typedef struct {
    bool is_read;
    bool ap;
    unsigned int addr;
    size_t count;
    uint32_t *values;
} swd_transaction_t;

static inline unsigned int parity32(uint32_t value)
{
    return (unsigned int)__builtin_parity(value);
}

static uint8_t make_request(bool ap, bool read, unsigned int addr)
{
    unsigned int fields = (unsigned int)ap | ((unsigned int)read << 1) | (((addr >> 2) & 3U) << 2);
    return (uint8_t)(1U | ((unsigned int)ap << 1) | ((unsigned int)read << 2)
        | (((addr >> 2) & 3U) << 3) | (parity32(fields) << 5) | (1U << 7));
}

static enum swd_status wire_read_once(
    swd_bus_t *bus, bool ap, unsigned int addr, uint32_t *value)
{
    uint8_t request = make_request(ap, true, addr);
    uint8_t zeros = 0;
    write_bits(bus, &request, 8);
    unsigned int ack = (unsigned int)(read_bits(bus, 4) >> 1) & 7U;
    if (ack != 1U) {
        (void)read_bits(bus, 1);
        write_bits(bus, &zeros, 8);
        if (ack == 2U)
            return SWD_STATUS_WAIT;
        if (ack == 4U)
            return SWD_STATUS_FAULT;
        return SWD_STATUS_PROTOCOL;
    }

    uint64_t raw = read_bits(bus, 34);
    write_bits(bus, &zeros, 3);
    *value = (uint32_t)raw;
    if (((raw >> 32) & 1U) != parity32(*value))
        return SWD_STATUS_PARITY;
    return SWD_STATUS_OK;
}

static enum swd_status wire_write_once(
    swd_bus_t *bus, bool ap, unsigned int addr, uint32_t value)
{
    uint8_t request = make_request(ap, false, addr);
    uint8_t zeros = 0;
    write_bits(bus, &request, 8);
    unsigned int ack = (unsigned int)(read_bits(bus, 5) >> 1) & 7U;
    bool is_targetsel = !ap && ((addr & 0xcU) == 0xcU);
    if (ack != 1U && !is_targetsel) {
        write_bits(bus, &zeros, 8);
        if (ack == 2U)
            return SWD_STATUS_WAIT;
        if (ack == 4U)
            return SWD_STATUS_FAULT;
        return SWD_STATUS_PROTOCOL;
    }

    uint8_t data[5] = {0};
    memcpy(data, &value, sizeof(value));
    if (parity32(value))
        data[4] |= 1U;
    write_bits(bus, data, 36);
    return SWD_STATUS_OK;
}

static enum swd_status wire_read(
    swd_bus_t *bus, bool ap, unsigned int addr, uint32_t *value, unsigned int retries)
{
    enum swd_status status;
    for (unsigned int retry = 0; retry <= retries; ++retry) {
        status = wire_read_once(bus, ap, addr, value);
        if (status != SWD_STATUS_WAIT)
            return status;
    }
    return SWD_STATUS_WAIT;
}

static enum swd_status wire_write(
    swd_bus_t *bus, bool ap, unsigned int addr, uint32_t value, unsigned int retries)
{
    enum swd_status status;
    for (unsigned int retry = 0; retry <= retries; ++retry) {
        status = wire_write_once(bus, ap, addr, value);
        if (status != SWD_STATUS_WAIT)
            return status;
    }
    return SWD_STATUS_WAIT;
}

static void free_transactions(swd_transaction_t *transactions, Py_ssize_t count)
{
    if (!transactions)
        return;
    for (Py_ssize_t index = 0; index < count; ++index)
        free(transactions[index].values);
    free(transactions);
}

static bool parse_transactions(
    PyObject *object, swd_transaction_t **result, Py_ssize_t *result_count)
{
    PyObject *items = PySequence_Fast(object, "transactions must be iterable");
    if (!items)
        return false;
    Py_ssize_t count = PySequence_Fast_GET_SIZE(items);
    swd_transaction_t *transactions = calloc((size_t)count, sizeof(*transactions));
    if (!transactions) {
        Py_DECREF(items);
        PyErr_NoMemory();
        return false;
    }

    for (Py_ssize_t index = 0; index < count; ++index) {
        PyObject *item = PySequence_Fast(PySequence_Fast_GET_ITEM(items, index),
            "each transaction must be iterable");
        if (!item)
            goto error;
        if (PySequence_Fast_GET_SIZE(item) != 4) {
            Py_DECREF(item);
            PyErr_SetString(PyExc_ValueError, "transactions must contain read, AP, address, and payload");
            goto error;
        }
        swd_transaction_t *transaction = &transactions[index];
        int is_read = PyObject_IsTrue(PySequence_Fast_GET_ITEM(item, 0));
        int ap = PyObject_IsTrue(PySequence_Fast_GET_ITEM(item, 1));
        if (is_read < 0 || ap < 0) {
            Py_DECREF(item);
            goto error;
        }
        transaction->is_read = is_read;
        transaction->ap = ap;
        unsigned long addr = PyLong_AsUnsignedLong(PySequence_Fast_GET_ITEM(item, 2));
        if (PyErr_Occurred() || addr > 0xffffffffUL) {
            Py_DECREF(item);
            goto error;
        }
        transaction->addr = (unsigned int)addr;
        PyObject *payload = PySequence_Fast_GET_ITEM(item, 3);
        if (transaction->is_read) {
            transaction->count = PyLong_AsSize_t(payload);
            if (PyErr_Occurred()) {
                Py_DECREF(item);
                goto error;
            }
            if (transaction->count > MAX_TRANSACTION_WORDS) {
                Py_DECREF(item);
                PyErr_SetString(PyExc_ValueError, "transaction word count is too large");
                goto error;
            }
        } else {
            PyObject *values = PySequence_Fast(payload, "write payload must be iterable");
            if (!values) {
                Py_DECREF(item);
                goto error;
            }
            transaction->count = (size_t)PySequence_Fast_GET_SIZE(values);
            if (transaction->count > MAX_TRANSACTION_WORDS) {
                Py_DECREF(values);
                Py_DECREF(item);
                PyErr_SetString(PyExc_ValueError, "transaction word count is too large");
                goto error;
            }
            transaction->values = calloc(transaction->count ? transaction->count : 1, sizeof(uint32_t));
            if (!transaction->values) {
                Py_DECREF(values);
                Py_DECREF(item);
                PyErr_NoMemory();
                goto error;
            }
            for (size_t value_index = 0; value_index < transaction->count; ++value_index) {
                unsigned long value = PyLong_AsUnsignedLong(
                    PySequence_Fast_GET_ITEM(values, (Py_ssize_t)value_index));
                if (PyErr_Occurred() || value > 0xffffffffUL) {
                    Py_DECREF(values);
                    Py_DECREF(item);
                    goto error;
                }
                transaction->values[value_index] = (uint32_t)value;
            }
            Py_DECREF(values);
        }
        if (transaction->is_read) {
            transaction->values = calloc(transaction->count ? transaction->count : 1, sizeof(uint32_t));
            if (!transaction->values) {
                Py_DECREF(item);
                PyErr_NoMemory();
                goto error;
            }
        }
        Py_DECREF(item);
    }
    Py_DECREF(items);
    *result = transactions;
    *result_count = count;
    return true;

error:
    Py_DECREF(items);
    free_transactions(transactions, count);
    return false;
}

static enum swd_status execute_transaction(
    swd_bus_t *bus, swd_transaction_t *transaction, unsigned int retries)
{
    if (!transaction->is_read) {
        for (size_t index = 0; index < transaction->count; ++index) {
            enum swd_status status = wire_write(bus, transaction->ap, transaction->addr,
                transaction->values[index], retries);
            if (status != SWD_STATUS_OK)
                return status;
        }
        return SWD_STATUS_OK;
    }

    if (!transaction->ap) {
        for (size_t index = 0; index < transaction->count; ++index) {
            enum swd_status status = wire_read(bus, false, transaction->addr,
                &transaction->values[index], retries);
            if (status != SWD_STATUS_OK)
                return status;
        }
        return SWD_STATUS_OK;
    }

    if (transaction->count == 0)
        return SWD_STATUS_OK;
    uint32_t discarded;
    enum swd_status status = wire_read(bus, true, transaction->addr, &discarded, retries);
    if (status != SWD_STATUS_OK)
        return status;
    for (size_t index = 0; index + 1 < transaction->count; ++index) {
        status = wire_read(bus, true, transaction->addr, &transaction->values[index], retries);
        if (status != SWD_STATUS_OK)
            return status;
    }
    return wire_read(bus, false, 0xc, &transaction->values[transaction->count - 1], retries);
}

static PyObject *BCMEngine_transactions(BCMEngine *self, PyObject *args)
{
    unsigned int swclk;
    unsigned int swdio;
    int swdio_dir;
    unsigned int retries;
    PyObject *transaction_object;
    if (!PyArg_ParseTuple(args, "IIiIO:transactions", &swclk, &swdio, &swdio_dir,
            &retries, &transaction_object)
        || !require_open(self))
        return NULL;

    swd_bus_t bus;
    if (!init_swd_bus(self, swclk, swdio, swdio_dir, &bus))
        return NULL;
    swd_transaction_t *transactions = NULL;
    Py_ssize_t transaction_count = 0;
    if (!parse_transactions(transaction_object, &transactions, &transaction_count))
        return NULL;

    enum swd_status status = SWD_STATUS_OK;
    Py_ssize_t failed_index = -1;
    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t index = 0; index < transaction_count; ++index) {
        status = execute_transaction(&bus, &transactions[index], retries);
        if (status != SWD_STATUS_OK) {
            failed_index = index;
            break;
        }
    }
    Py_END_ALLOW_THREADS

    if (status != SWD_STATUS_OK) {
        PyObject *exception = status == SWD_STATUS_WAIT ? SWDWaitError
            : status == SWD_STATUS_FAULT ? SWDFaultError : SWDProtocolError;
        PyErr_Format(exception, "SWD transaction %zd failed%s", failed_index,
            status == SWD_STATUS_PARITY ? " parity check" : "");
        free_transactions(transactions, transaction_count);
        return NULL;
    }

    PyObject *results = PyList_New(transaction_count);
    if (!results) {
        free_transactions(transactions, transaction_count);
        return NULL;
    }
    for (Py_ssize_t index = 0; index < transaction_count; ++index) {
        swd_transaction_t *transaction = &transactions[index];
        if (!transaction->is_read) {
            Py_INCREF(Py_None);
            PyList_SET_ITEM(results, index, Py_None);
            continue;
        }
        PyObject *values = PyList_New((Py_ssize_t)transaction->count);
        if (!values) {
            Py_DECREF(results);
            free_transactions(transactions, transaction_count);
            return NULL;
        }
        for (size_t value_index = 0; value_index < transaction->count; ++value_index) {
            PyObject *value = PyLong_FromUnsignedLong(transaction->values[value_index]);
            if (!value) {
                Py_DECREF(values);
                Py_DECREF(results);
                free_transactions(transactions, transaction_count);
                return NULL;
            }
            PyList_SET_ITEM(values, (Py_ssize_t)value_index, value);
        }
        PyList_SET_ITEM(results, index, values);
    }
    free_transactions(transactions, transaction_count);
    return results;
}

static PyMethodDef BCMEngine_methods[] = {
    {"open", (PyCFunction)BCMEngine_open, METH_VARARGS, "Open and map a gpiomem device."},
    {"close", (PyCFunction)BCMEngine_close, METH_NOARGS, "Close the GPIO mapping."},
    {"set_frequency", (PyCFunction)BCMEngine_set_frequency, METH_VARARGS, "Set SWD frequency."},
    {"get_mode", (PyCFunction)BCMEngine_get_mode, METH_VARARGS, "Read a GPIO function."},
    {"set_mode", (PyCFunction)BCMEngine_set_mode, METH_VARARGS, "Set a GPIO function."},
    {"write", (PyCFunction)BCMEngine_write, METH_VARARGS, "Write a GPIO output latch."},
    {"read", (PyCFunction)BCMEngine_read, METH_VARARGS, "Read a GPIO level."},
    {"transfer", (PyCFunction)BCMEngine_transfer, METH_VARARGS, "Execute batched SWD sequences."},
    {"transactions", (PyCFunction)BCMEngine_transactions, METH_VARARGS,
        "Execute queued complete SWD register transactions."},
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef BCMEngine_getset[] = {
    {"is_open", (getter)BCMEngine_get_is_open, NULL, "Whether gpiomem is mapped.", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject BCMEngineType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pyocd.probe.raspberry_pi._bcm_gpio.BCMEngine",
    .tp_basicsize = sizeof(BCMEngine),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = BCMEngine_new,
    .tp_dealloc = (destructor)BCMEngine_dealloc,
    .tp_methods = BCMEngine_methods,
    .tp_getset = BCMEngine_getset,
    .tp_doc = "Native Broadcom GPIO SWD engine.",
};

static PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_bcm_gpio",
    .m_doc = "Native Broadcom GPIO access for the Raspberry Pi SWD probe.",
    .m_size = -1,
};

PyMODINIT_FUNC PyInit__bcm_gpio(void)
{
    if (PyType_Ready(&BCMEngineType) < 0)
        return NULL;
    PyObject *result = PyModule_Create(&module);
    if (!result)
        return NULL;
    SWDError = PyErr_NewException("pyocd.probe.raspberry_pi._bcm_gpio.SWDError", NULL, NULL);
    SWDWaitError = PyErr_NewException("pyocd.probe.raspberry_pi._bcm_gpio.SWDWaitError", SWDError, NULL);
    SWDFaultError = PyErr_NewException("pyocd.probe.raspberry_pi._bcm_gpio.SWDFaultError", SWDError, NULL);
    SWDProtocolError = PyErr_NewException("pyocd.probe.raspberry_pi._bcm_gpio.SWDProtocolError", SWDError, NULL);
    if (!SWDError || !SWDWaitError || !SWDFaultError || !SWDProtocolError) {
        Py_DECREF(result);
        return NULL;
    }
    Py_INCREF(SWDError);
    Py_INCREF(SWDWaitError);
    Py_INCREF(SWDFaultError);
    Py_INCREF(SWDProtocolError);
    if (PyModule_AddObject(result, "SWDError", SWDError) < 0
        || PyModule_AddObject(result, "SWDWaitError", SWDWaitError) < 0
        || PyModule_AddObject(result, "SWDFaultError", SWDFaultError) < 0
        || PyModule_AddObject(result, "SWDProtocolError", SWDProtocolError) < 0) {
        Py_DECREF(result);
        return NULL;
    }
    Py_INCREF(&BCMEngineType);
    if (PyModule_AddObject(result, "BCMEngine", (PyObject *)&BCMEngineType) < 0) {
        Py_DECREF(&BCMEngineType);
        Py_DECREF(result);
        return NULL;
    }
    return result;
}
