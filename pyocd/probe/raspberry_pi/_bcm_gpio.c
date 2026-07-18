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

typedef struct {
    PyObject_HEAD
    int fd;
    volatile uint32_t *registers;
    uint64_t half_period_ns;
} BCMEngine;

typedef struct {
    bool is_read;
    unsigned int count;
    uint8_t *data;
} swd_operation_t;

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

static inline void edge_delay(BCMEngine *self)
{
    if (self->half_period_ns == 0)
        return;
    uint64_t deadline = monotonic_ns() + self->half_period_ns;
    while (monotonic_ns() < deadline) {
    }
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
    for (Py_ssize_t operation_index = 0; operation_index < operation_count; ++operation_index) {
        swd_operation_t *operation = &operations[operation_index];
        if (operation->is_read) {
            gpio_set_mode(self, swdio, MODE_INPUT);
            if (swdio_dir >= 0)
                gpio_write(self, (unsigned int)swdio_dir, false);
            gpio_sync();
        } else if (operation->count > 0) {
            bool first_bit = (operation->data[0] & 1) != 0;
            gpio_write(self, swdio, first_bit);
            gpio_set_mode(self, swdio, MODE_OUTPUT);
            if (swdio_dir >= 0)
                gpio_write(self, (unsigned int)swdio_dir, true);
            gpio_sync();
        }

        for (unsigned int bit_index = 0; bit_index < operation->count; ++bit_index) {
            gpio_write(self, swclk, false);
            if (!operation->is_read) {
                bool bit = (operation->data[bit_index / 8] & (1U << (bit_index % 8))) != 0;
                gpio_write(self, swdio, bit);
            }
            gpio_sync();
            edge_delay(self);

            if (operation->is_read && gpio_read(self, swdio))
                operation->data[bit_index / 8] |= (uint8_t)(1U << (bit_index % 8));

            gpio_write(self, swclk, true);
            gpio_sync();
            edge_delay(self);
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

static PyMethodDef BCMEngine_methods[] = {
    {"open", (PyCFunction)BCMEngine_open, METH_VARARGS, "Open and map a gpiomem device."},
    {"close", (PyCFunction)BCMEngine_close, METH_NOARGS, "Close the GPIO mapping."},
    {"set_frequency", (PyCFunction)BCMEngine_set_frequency, METH_VARARGS, "Set SWD frequency."},
    {"get_mode", (PyCFunction)BCMEngine_get_mode, METH_VARARGS, "Read a GPIO function."},
    {"set_mode", (PyCFunction)BCMEngine_set_mode, METH_VARARGS, "Set a GPIO function."},
    {"write", (PyCFunction)BCMEngine_write, METH_VARARGS, "Write a GPIO output latch."},
    {"read", (PyCFunction)BCMEngine_read, METH_VARARGS, "Read a GPIO level."},
    {"transfer", (PyCFunction)BCMEngine_transfer, METH_VARARGS, "Execute batched SWD sequences."},
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
    Py_INCREF(&BCMEngineType);
    if (PyModule_AddObject(result, "BCMEngine", (PyObject *)&BCMEngineType) < 0) {
        Py_DECREF(&BCMEngineType);
        Py_DECREF(result);
        return NULL;
    }
    return result;
}
