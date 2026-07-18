# pyOCD debugger
# Copyright (c) 2026 Arm Limited
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ctypes
import mmap
import os
import time
from typing import Dict, Iterable, Optional, Tuple

from ...core import exceptions


class BCMGPIO:
    """Direct GPIO access through the Raspberry Pi ``/dev/gpiomem`` device.

    This backend supports the Broadcom GPIO controller used by Raspberry Pi models
    through Pi 4, including Pi Zero W and Pi Zero 2 W. The kernel's gpiomem device
    maps the GPIO register block at offset zero, so no SoC peripheral base address
    is required.
    """

    MAP_SIZE = 4096
    PIN_COUNT = 54

    # Register indices (32-bit words) in the GPIO register block.
    _GPFSEL0 = 0
    _GPSET0 = 7
    _GPCLR0 = 10
    _GPLEV0 = 13

    _MODE_INPUT = 0
    _MODE_OUTPUT = 1

    _DEVICE_TREE_COMPATIBLE = "/proc/device-tree/compatible"

    def __init__(self, device: str = "/dev/gpiomem") -> None:
        self._device = device
        self._fd: Optional[int] = None
        self._map: Optional[mmap.mmap] = None
        self._registers = None
        self._saved_pins: Dict[int, Tuple[int, bool]] = {}
        self._half_period_ns = 500

    @property
    def is_open(self) -> bool:
        return self._map is not None

    def open(self) -> None:
        if self.is_open:
            return
        if self._is_raspberry_pi_5():
            raise exceptions.ProbeError(
                "Raspberry Pi 5 GPIO is not supported by the Broadcom mmap backend"
            )
        try:
            self._fd = os.open(self._device, os.O_RDWR | os.O_SYNC)
            self._map = mmap.mmap(
                self._fd,
                self.MAP_SIZE,
                flags=mmap.MAP_SHARED,
                prot=mmap.PROT_READ | mmap.PROT_WRITE,
                offset=0,
            )
            register_type = ctypes.c_uint32 * (self.MAP_SIZE // ctypes.sizeof(ctypes.c_uint32))
            self._registers = register_type.from_buffer(self._map)
        except (OSError, ValueError) as error:
            self.close()
            raise exceptions.ProbeError(
                f"unable to access Raspberry Pi GPIO through {self._device}: {error}"
            ) from error

    def close(self) -> None:
        # The ctypes view must be released before the mmap can be closed.
        self._registers = None
        if self._map is not None:
            self._map.close()
            self._map = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def set_frequency(self, frequency: float) -> None:
        if frequency <= 0:
            raise ValueError("SWD frequency must be greater than zero")
        self._half_period_ns = max(0, int(500_000_000 / frequency))

    def save_pins(self, pins: Iterable[Optional[int]]) -> None:
        for pin in pins:
            if pin is None or pin in self._saved_pins:
                continue
            self._validate_pin(pin)
            self._saved_pins[pin] = (self.get_mode(pin), self.read(pin))

    def restore_pins(self) -> None:
        for pin, (mode, level) in reversed(tuple(self._saved_pins.items())):
            # Preload the output latch before restoring output mode to avoid a glitch.
            if mode == self._MODE_OUTPUT:
                self.write(pin, level)
            self.set_mode(pin, mode)
        self._saved_pins.clear()

    def get_mode(self, pin: int) -> int:
        self._require_open()
        self._validate_pin(pin)
        shift = (pin % 10) * 3
        return (self._registers[self._GPFSEL0 + pin // 10] >> shift) & 0x7

    def set_mode(self, pin: int, mode: int) -> None:
        self._require_open()
        self._validate_pin(pin)
        if not 0 <= mode <= 7:
            raise ValueError(f"invalid GPIO function {mode}")
        index = self._GPFSEL0 + pin // 10
        shift = (pin % 10) * 3
        value = self._registers[index]
        self._registers[index] = (value & ~(0x7 << shift)) | (mode << shift)

    def set_input(self, pin: int) -> None:
        self.set_mode(pin, self._MODE_INPUT)

    def set_output(self, pin: int, initial: bool) -> None:
        # Setting the latch first prevents a transient when output mode is enabled.
        self.write(pin, initial)
        self.set_mode(pin, self._MODE_OUTPUT)

    def write(self, pin: int, value: bool) -> None:
        self._require_open()
        self._validate_pin(pin)
        register = (self._GPSET0 if value else self._GPCLR0) + pin // 32
        self._registers[register] = 1 << (pin % 32)

    def read(self, pin: int) -> bool:
        self._require_open()
        self._validate_pin(pin)
        value = self._registers[self._GPLEV0 + pin // 32]
        return bool(value & (1 << (pin % 32)))

    def swd_write_bits(self, swclk: int, swdio: int, value: int, count: int) -> None:
        if count < 0:
            raise ValueError("bit count cannot be negative")
        if count == 0:
            return
        self.write(swdio, bool(value & 1))
        self.set_mode(swdio, self._MODE_OUTPUT)
        for bit_index in range(count):
            bit = bool(value & (1 << bit_index))
            self.write(swclk, False)
            self.write(swdio, bit)
            self._delay()
            self.write(swclk, True)
            self._delay()

    def swd_read_bits(self, swclk: int, swdio: int, count: int) -> int:
        if count < 0:
            raise ValueError("bit count cannot be negative")
        self.set_input(swdio)
        result = 0
        for bit_index in range(count):
            self.write(swclk, False)
            self._delay()
            self.write(swclk, True)
            if self.read(swdio):
                result |= 1 << bit_index
            self._delay()
        return result

    def _delay(self) -> None:
        if self._half_period_ns == 0:
            return
        deadline = time.perf_counter_ns() + self._half_period_ns
        while time.perf_counter_ns() < deadline:
            pass

    def _require_open(self) -> None:
        if not self.is_open:
            raise exceptions.ProbeError("Raspberry Pi GPIO is not open")

    @classmethod
    def _is_raspberry_pi_5(cls) -> bool:
        try:
            with open(cls._DEVICE_TREE_COMPATIBLE, "rb") as compatible_file:
                return b"brcm,bcm2712" in compatible_file.read()
        except OSError:
            return False

    @classmethod
    def _validate_pin(cls, pin: int) -> None:
        if not isinstance(pin, int) or isinstance(pin, bool) or not 0 <= pin < cls.PIN_COUNT:
            raise ValueError(f"GPIO number must be between 0 and {cls.PIN_COUNT - 1}")
