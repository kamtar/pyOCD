# pyOCD debugger
# Copyright (c) 2026 Arm Limited
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from time import sleep
from typing import Callable, Optional, Sequence, Set, Tuple, Union

from ..debug_probe import DebugProbe
from .gpio import (BCMGPIO, create_gpio_backend)
from ...core import exceptions
from ...core.options import OptionInfo
from ...core.plugin import Plugin

LOG = logging.getLogger(__name__)


class RaspberryPiProbe(DebugProbe):
    """SWD debug probe using the GPIO header of a Broadcom-based Raspberry Pi."""

    UNIQUE_ID = "0"

    # SWD request and response constants.
    _ACK_OK = 0b001
    _ACK_WAIT = 0b010
    _ACK_FAULT = 0b100
    _DP_RDBUFF = 0xC

    _OPTION_PREFIX = "rpi_gpio."

    @classmethod
    def get_all_connected_probes(cls, unique_id=None, is_explicit=False) -> Sequence[DebugProbe]:
        # GPIO is a host resource, not a discoverable peripheral. Only advertise it
        # when the user explicitly selects this probe type.
        if not is_explicit:
            return []
        if unique_id not in (None, "", cls.UNIQUE_ID):
            return []
        return [cls()]

    @classmethod
    def get_probe_with_id(cls, unique_id: str, is_explicit: bool = False) -> Optional[DebugProbe]:
        if not is_explicit or unique_id not in (None, "", cls.UNIQUE_ID):
            return None
        return cls()

    def __init__(self, gpio: Optional[BCMGPIO] = None) -> None:
        super().__init__()
        self._gpio = gpio
        self._is_open = False
        self._protocol: Optional[DebugProbe.Protocol] = None
        self._swclk = 20
        self._swdio = 21
        self._nreset: Optional[int] = None
        self._swdio_dir: Optional[int] = None
        self._reset_asserted = False
        self._restore_pins = True
        self._wait_retries = 50

    @property
    def vendor_name(self) -> str:
        return "Raspberry Pi"

    @property
    def product_name(self) -> str:
        return "GPIO SWD"

    @property
    def supported_wire_protocols(self):
        return [DebugProbe.Protocol.DEFAULT, DebugProbe.Protocol.SWD]

    @property
    def unique_id(self) -> str:
        return self.UNIQUE_ID

    @property
    def wire_protocol(self) -> Optional[DebugProbe.Protocol]:
        return self._protocol

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def capabilities(self) -> Set[DebugProbe.Capability]:
        return {
            DebugProbe.Capability.SWJ_SEQUENCE,
            DebugProbe.Capability.SWD_SEQUENCE,
            DebugProbe.Capability.PIN_ACCESS,
        }

    def open(self) -> None:
        if self._is_open:
            return
        assert self.session is not None
        options = self.session.options
        self._swclk = options.get(self._OPTION_PREFIX + "swclk")
        self._swdio = options.get(self._OPTION_PREFIX + "swdio")
        self._nreset = options.get(self._OPTION_PREFIX + "nreset")
        self._swdio_dir = options.get(self._OPTION_PREFIX + "swdio_dir")
        self._restore_pins = options.get(self._OPTION_PREFIX + "restore_pins")
        self._wait_retries = options.get(self._OPTION_PREFIX + "wait_retries")

        if self._gpio is None:
            self._gpio = create_gpio_backend(
                options.get(self._OPTION_PREFIX + "backend"),
                options.get(self._OPTION_PREFIX + "device"),
            )

        pins = [self._swclk, self._swdio, self._nreset, self._swdio_dir]
        configured_pins = [pin for pin in pins if pin is not None]
        if len(configured_pins) != len(set(configured_pins)):
            raise exceptions.ProbeError("Raspberry Pi SWD GPIO assignments must be unique")

        try:
            assert self._gpio is not None
            self._gpio.open()
            self._gpio.save_pins(pins)
            self._gpio.set_frequency(options.get("frequency"))
            self._gpio.set_output(self._swclk, False)
            self._gpio.set_output(self._swdio, True)
            if self._swdio_dir is not None:
                self._gpio.set_output(self._swdio_dir, True)
            if self._nreset is not None:
                # nRESET is released by making the GPIO high impedance.
                self._gpio.write(self._nreset, False)
                self._gpio.set_input(self._nreset)
            self._is_open = True
            LOG.info("Using Raspberry Pi GPIO %s backend", self._gpio.backend_name)
            if self._gpio.backend_name == "python":
                LOG.warning(
                    "Native Raspberry Pi GPIO extension is unavailable; SWD performance will be limited"
                )
        except Exception:
            self._cleanup_gpio()
            raise

    def close(self) -> None:
        self._protocol = None
        self._cleanup_gpio()

    def connect(self, protocol: Optional[DebugProbe.Protocol] = None) -> None:
        if protocol in (None, DebugProbe.Protocol.DEFAULT):
            protocol = DebugProbe.Protocol.SWD
        if protocol is not DebugProbe.Protocol.SWD:
            raise ValueError(f"unsupported wire protocol {protocol}")
        if not self._is_open:
            raise exceptions.ProbeError("Raspberry Pi GPIO probe is not open")
        self._protocol = protocol

    def disconnect(self) -> None:
        self._protocol = None

    def set_clock(self, frequency: float) -> None:
        assert self._gpio is not None
        self._gpio.set_frequency(frequency)

    def swj_sequence(self, length: int, bits: int) -> None:
        self.swd_sequence(((length, bits),))

    def swd_sequence(self, sequences) -> Tuple[int, Sequence[bytes]]:
        assert self._gpio is not None
        reads = self._gpio.execute_swd_sequences(
            self._swclk, self._swdio, self._swdio_dir, sequences)
        return 0, reads

    def reset(self) -> None:
        self.assert_reset(True)
        sleep(self.session.options.get("reset.hold_time"))
        self.assert_reset(False)
        sleep(self.session.options.get("reset.post_delay"))

    def assert_reset(self, asserted: bool) -> None:
        if self._nreset is None:
            raise exceptions.ProbeError("rpi_gpio.nreset is not configured")
        if asserted:
            self._gpio.set_output(self._nreset, False)
        else:
            self._gpio.set_input(self._nreset)
        self._reset_asserted = asserted

    def is_reset_asserted(self) -> bool:
        return self._reset_asserted

    def get_accessible_pins(self, group: DebugProbe.PinGroup) -> Tuple[int, int]:
        if group is not DebugProbe.PinGroup.PROTOCOL_PINS:
            return 0, 0
        pins = DebugProbe.ProtocolPin.SWCLK_TCK | DebugProbe.ProtocolPin.SWDIO_TMS
        if self._nreset is not None:
            pins |= DebugProbe.ProtocolPin.nRESET
        return int(pins), int(pins)

    def read_pins(self, group: DebugProbe.PinGroup, mask: int) -> int:
        if group is not DebugProbe.PinGroup.PROTOCOL_PINS:
            raise ValueError("only protocol pin access is supported")
        result = 0
        if mask & DebugProbe.ProtocolPin.SWCLK_TCK and self._gpio.read(self._swclk):
            result |= DebugProbe.ProtocolPin.SWCLK_TCK
        if mask & DebugProbe.ProtocolPin.SWDIO_TMS and self._gpio.read(self._swdio):
            result |= DebugProbe.ProtocolPin.SWDIO_TMS
        if mask & DebugProbe.ProtocolPin.nRESET and self._nreset is not None and self._gpio.read(self._nreset):
            result |= DebugProbe.ProtocolPin.nRESET
        return int(result)

    def write_pins(self, group: DebugProbe.PinGroup, mask: int, value: int) -> None:
        if group is not DebugProbe.PinGroup.PROTOCOL_PINS:
            raise ValueError("only protocol pin access is supported")
        if mask & DebugProbe.ProtocolPin.SWCLK_TCK:
            self._gpio.write(self._swclk, bool(value & DebugProbe.ProtocolPin.SWCLK_TCK))
        if mask & DebugProbe.ProtocolPin.SWDIO_TMS:
            self._set_swdio_output(True)
            self._gpio.write(self._swdio, bool(value & DebugProbe.ProtocolPin.SWDIO_TMS))
        if mask & DebugProbe.ProtocolPin.nRESET and self._nreset is not None:
            self.assert_reset(not bool(value & DebugProbe.ProtocolPin.nRESET))

    def read_dp(self, addr: int, now: bool = True) -> Union[int, Callable[[], int]]:
        value = self._read_reg(False, addr)
        return value if now else lambda: value

    def write_dp(self, addr: int, data: int) -> None:
        self._write_reg(False, addr, data)

    def read_ap(self, addr: int, now: bool = True) -> Union[int, Callable[[], int]]:
        value = self.read_ap_multiple(addr, 1, now=True)[0]
        return value if now else lambda: value

    def write_ap(self, addr: int, data: int) -> None:
        self._write_reg(True, addr, data)

    def read_ap_multiple(self, addr: int, count: int = 1, now: bool = True):
        if count < 1:
            values = []
        else:
            self._read_reg(True, addr)  # Discard the pipelined value.
            values = [self._read_reg(True, addr) for _ in range(count - 1)]
            values.append(self._read_reg(False, self._DP_RDBUFF))
        return values if now else lambda: values

    def write_ap_multiple(self, addr: int, values) -> None:
        for value in values:
            self._write_reg(True, addr, value)

    def _read_reg(self, ap: bool, addr: int) -> int:
        request = self._make_request(ap, True, addr)
        for _ in range(self._wait_retries + 1):
            _, response = self.swd_sequence(((8, request), (4,)))
            ack = (int.from_bytes(response[0], "little") >> 1) & 0x7
            if ack == self._ACK_OK:
                _, response = self.swd_sequence(((34,), (3, 0)))
                raw = int.from_bytes(response[0], "little")
                value = raw & 0xFFFFFFFF
                parity = (raw >> 32) & 1
                if parity != (value.bit_count() & 1):
                    raise exceptions.TransferProtocolError("SWD read parity error")
                return value
            self._finish_failed_read()
            if ack == self._ACK_FAULT:
                raise exceptions.TransferFaultError("Raspberry Pi GPIO SWD FAULT response")
            if ack != self._ACK_WAIT:
                raise exceptions.TransferProtocolError(f"invalid SWD ACK {ack:#x}")
        raise exceptions.TransferTimeoutError("Raspberry Pi GPIO SWD WAIT timeout")

    def _write_reg(self, ap: bool, addr: int, value: int) -> None:
        request = self._make_request(ap, False, addr)
        data = value | ((value.bit_count() & 1) << 32)
        for _ in range(self._wait_retries + 1):
            _, response = self.swd_sequence(((8, request), (5,)))
            ack = (int.from_bytes(response[0], "little") >> 1) & 0x7
            # A DP write to address 0xc is TARGETSEL and does not return an ACK.
            if ack == self._ACK_OK or (not ap and (addr & 0xC) == 0xC):
                self.swd_sequence(((36, data),))
                return
            self.swd_sequence(((8, 0),))
            if ack == self._ACK_FAULT:
                raise exceptions.TransferFaultError("Raspberry Pi GPIO SWD FAULT response")
            if ack != self._ACK_WAIT:
                raise exceptions.TransferProtocolError(f"invalid SWD ACK {ack:#x}")
        raise exceptions.TransferTimeoutError("Raspberry Pi GPIO SWD WAIT timeout")

    def _finish_failed_read(self) -> None:
        # One target-to-host turnaround cycle followed by host-driven idle cycles.
        self.swd_sequence(((1,), (8, 0)))

    @staticmethod
    def _make_request(ap: bool, read: bool, addr: int) -> int:
        fields = int(ap) | (int(read) << 1) | (((addr >> 2) & 0x3) << 2)
        parity = fields.bit_count() & 1
        return 1 | (int(ap) << 1) | (int(read) << 2) \
            | (((addr >> 2) & 0x3) << 3) | (parity << 5) | (1 << 7)

    def _set_swdio_output(self, output: bool) -> None:
        assert self._gpio is not None
        if output:
            self._gpio.set_mode(self._swdio, self._gpio._MODE_OUTPUT)
            if self._swdio_dir is not None:
                self._gpio.write(self._swdio_dir, True)
        else:
            self._gpio.set_input(self._swdio)
            if self._swdio_dir is not None:
                self._gpio.write(self._swdio_dir, False)

    def _cleanup_gpio(self) -> None:
        if self._gpio is not None and self._gpio.is_open:
            try:
                if self._restore_pins:
                    self._gpio.restore_pins()
            finally:
                self._gpio.close()
        self._is_open = False


class RaspberryPiProbePlugin(Plugin):
    def load(self):
        return RaspberryPiProbe

    @property
    def name(self) -> str:
        return "rpi-gpio"

    @property
    def description(self) -> str:
        return "Raspberry Pi GPIO SWD probe"

    @property
    def options(self):
        return [
            OptionInfo("rpi_gpio.backend", str, "auto",
                "GPIO backend: auto, native, or python."),
            OptionInfo("rpi_gpio.device", str, "/dev/gpiomem",
                "Path to the Raspberry Pi gpiomem device."),
            OptionInfo("rpi_gpio.swclk", int, 20, "BCM GPIO number used for SWCLK."),
            OptionInfo("rpi_gpio.swdio", int, 21, "BCM GPIO number used for SWDIO."),
            OptionInfo("rpi_gpio.nreset", (int, type(None)), None,
                "Optional BCM GPIO number used for open-drain nRESET."),
            OptionInfo("rpi_gpio.swdio_dir", (int, type(None)), None,
                "Optional BCM GPIO controlling an external SWDIO direction buffer."),
            OptionInfo("rpi_gpio.restore_pins", bool, True,
                "Restore GPIO functions and levels when the probe closes."),
            OptionInfo("rpi_gpio.wait_retries", int, 50,
                "Number of times to retry an SWD transfer returning WAIT."),
        ]
