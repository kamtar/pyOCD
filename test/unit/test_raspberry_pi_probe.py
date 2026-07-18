# pyOCD debugger
# Copyright (c) 2026 Arm Limited
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest

from pyocd.core import exceptions
from pyocd.probe.debug_probe import DebugProbe
from pyocd.probe.raspberry_pi import gpio as gpio_module
from pyocd.probe.raspberry_pi.gpio import BCMGPIO
from pyocd.probe.raspberry_pi.gpio import (NativeBCMGPIO, create_gpio_backend)
from pyocd.probe.raspberry_pi.probe import RaspberryPiProbe


class MockGPIO:
    def __init__(self):
        self.is_open = False
        self.calls = []
        self.levels = {}

    def open(self):
        self.is_open = True

    @property
    def backend_name(self):
        return "mock"

    def close(self):
        self.is_open = False

    def save_pins(self, pins):
        self.calls.append(("save", tuple(pins)))

    def restore_pins(self):
        self.calls.append(("restore",))

    def set_frequency(self, frequency):
        self.calls.append(("frequency", frequency))

    def set_output(self, pin, initial):
        self.levels[pin] = initial
        self.calls.append(("output", pin, initial))

    def set_input(self, pin):
        self.calls.append(("input", pin))

    def set_mode(self, pin, mode):
        self.calls.append(("mode", pin, mode))

    def write(self, pin, value):
        self.levels[pin] = value
        self.calls.append(("write", pin, value))

    def read(self, pin):
        return self.levels.get(pin, False)

    def swd_write_bits(self, swclk, swdio, value, count):
        self.calls.append(("write_bits", swclk, swdio, value, count))

    def swd_read_bits(self, swclk, swdio, count):
        self.calls.append(("read_bits", swclk, swdio, count))
        return (1 << count) - 1

    def execute_swd_sequences(self, swclk, swdio, swdio_dir, sequences):
        reads = []
        for sequence in sequences:
            if len(sequence) == 1:
                value = self.swd_read_bits(swclk, swdio, sequence[0])
                reads.append(value.to_bytes((sequence[0] + 7) // 8, "little"))
            else:
                self.swd_write_bits(swclk, swdio, sequence[1], sequence[0])
        return reads


class ScriptedProbe(RaspberryPiProbe):
    def __init__(self, responses):
        super().__init__(MockGPIO())
        self.responses = iter(responses)
        self.sequences = []
        self._wait_retries = 1

    def swd_sequence(self, sequences):
        self.sequences.append(sequences)
        read_count = sum(1 for sequence in sequences if len(sequence) == 1)
        if read_count:
            response = next(self.responses)
            assert len(response) == read_count
            return 0, response
        return 0, []


class OrderingGPIO(BCMGPIO):
    def __init__(self):
        super().__init__()
        self.events = []

    def set_input(self, pin):
        self.events.append(("input", pin))

    def write(self, pin, value):
        self.events.append(("write", pin, value))

    def read(self, pin):
        self.events.append(("read", pin))
        return True

    def _delay(self):
        self.events.append(("delay",))


def make_session(**overrides):
    options = {
        "rpi_gpio.swclk": 11,
        "rpi_gpio.swdio": 8,
        "rpi_gpio.nreset": None,
        "rpi_gpio.swdio_dir": None,
        "rpi_gpio.restore_pins": True,
        "rpi_gpio.wait_retries": 50,
        "frequency": 100_000,
        "reset.hold_time": 0,
        "reset.post_delay": 0,
    }
    options.update(overrides)
    return SimpleNamespace(options=options)


def test_swd_request_encoding():
    assert RaspberryPiProbe._make_request(False, True, 0x0) == 0xA5
    assert RaspberryPiProbe._make_request(True, True, 0x0) == 0x87
    assert RaspberryPiProbe._make_request(False, False, 0x0) == 0x81
    assert RaspberryPiProbe._make_request(True, False, 0x0) == 0xA3


def test_gpio_probe_is_explicit_only():
    assert RaspberryPiProbe.get_all_connected_probes(is_explicit=False) == []
    assert len(RaspberryPiProbe.get_all_connected_probes(unique_id="", is_explicit=True)) == 1
    assert RaspberryPiProbe.get_probe_with_id("other", is_explicit=True) is None


def test_bcm_gpio_register_operations():
    gpio = BCMGPIO()
    gpio._map = object()
    gpio._registers = [0] * (BCMGPIO.MAP_SIZE // 4)

    gpio.set_output(11, True)
    assert gpio.get_mode(11) == 1
    assert gpio._registers[BCMGPIO._GPSET0] == 1 << 11

    gpio._registers[BCMGPIO._GPLEV0] = 1 << 11
    assert gpio.read(11)


def test_backend_auto_falls_back_to_python(monkeypatch):
    monkeypatch.setattr(gpio_module, "BCMEngine", None)
    assert create_gpio_backend("auto").backend_name == "python"
    with pytest.raises(exceptions.ProbeError):
        create_gpio_backend("native")


def test_native_backend_batches_and_encodes_sequences(monkeypatch):
    class FakeEngine:
        def __init__(self):
            self.is_open = False
            self.transfer_args = None

        def transfer(self, *args):
            self.transfer_args = args
            return [b"\x5a"]

    monkeypatch.setattr(gpio_module, "BCMEngine", FakeEngine)
    gpio = NativeBCMGPIO()
    reads = gpio.execute_swd_sequences(20, 21, None, ((9, 0x1a5), (8,)))
    assert reads == [b"\x5a"]
    assert gpio._engine.transfer_args == (20, 21, -1, [(9, b"\xa5\x01"), (8,)])


def test_swd_input_is_sampled_while_clock_is_low():
    gpio = OrderingGPIO()
    assert gpio.swd_read_bits(11, 8, 1) == 1
    assert gpio.events == [
        ("input", 8),
        ("write", 11, False),
        ("delay",),
        ("read", 8),
        ("write", 11, True),
        ("delay",),
    ]


def test_open_connect_and_restore():
    gpio = MockGPIO()
    probe = RaspberryPiProbe(gpio)
    probe.session = make_session(**{"rpi_gpio.nreset": 24})

    probe.open()
    probe.connect(DebugProbe.Protocol.SWD)
    assert probe.is_open
    assert probe.wire_protocol is DebugProbe.Protocol.SWD
    assert ("save", (11, 8, 24, None)) in gpio.calls
    assert ("input", 24) in gpio.calls

    probe.close()
    assert not probe.is_open
    assert ("restore",) in gpio.calls


def test_duplicate_pins_are_rejected():
    gpio = MockGPIO()
    probe = RaspberryPiProbe(gpio)
    probe.session = make_session(**{"rpi_gpio.swdio": 11})
    with pytest.raises(exceptions.ProbeError):
        probe.open()
    assert not gpio.is_open


def test_swd_sequence_returns_lsb_first_bytes():
    gpio = MockGPIO()
    probe = RaspberryPiProbe(gpio)
    status, reads = probe.swd_sequence(((5, 0b10101), (9,)))
    assert status == 0
    assert reads == [b"\xff\x01"]
    assert ("write_bits", 20, 21, 0b10101, 5) in gpio.calls
    assert ("read_bits", 20, 21, 9) in gpio.calls


def test_swj_sequence_uses_batched_backend():
    gpio = MockGPIO()
    probe = RaspberryPiProbe(gpio)
    probe.swj_sequence(16, 0xE79E)
    assert gpio.calls == [("write_bits", 20, 21, 0xE79E, 16)]


def test_read_dp_checks_ack_and_parity():
    value = 0x12345678
    raw_data = value | ((value.bit_count() & 1) << 32)
    probe = ScriptedProbe([
        [(RaspberryPiProbe._ACK_OK << 1).to_bytes(1, "little")],
        [raw_data.to_bytes(5, "little")],
    ])
    assert probe.read_dp(0) == value
    assert probe.sequences[0] == ((8, 0xA5), (4,))
    assert probe.sequences[1] == ((34,), (3, 0))


def test_read_dp_retries_wait():
    value = 0x2BA01477
    raw_data = value | ((value.bit_count() & 1) << 32)
    probe = ScriptedProbe([
        [(RaspberryPiProbe._ACK_WAIT << 1).to_bytes(1, "little")],
        [b"\x00"],
        [(RaspberryPiProbe._ACK_OK << 1).to_bytes(1, "little")],
        [raw_data.to_bytes(5, "little")],
    ])
    assert probe.read_dp(0) == value


def test_read_dp_reports_bad_parity():
    value = 0x12345678
    bad_data = value | (((value.bit_count() + 1) & 1) << 32)
    probe = ScriptedProbe([
        [(RaspberryPiProbe._ACK_OK << 1).to_bytes(1, "little")],
        [bad_data.to_bytes(5, "little")],
    ])
    with pytest.raises(exceptions.TransferProtocolError):
        probe.read_dp(0)


def test_write_dp_data_and_parity():
    value = 0xA5A5A5A5
    probe = ScriptedProbe([
        [(RaspberryPiProbe._ACK_OK << 1).to_bytes(1, "little")],
    ])
    probe.write_dp(0, value)
    expected = value | ((value.bit_count() & 1) << 32)
    assert probe.sequences == [((8, 0x81), (5,)), ((36, expected),)]
