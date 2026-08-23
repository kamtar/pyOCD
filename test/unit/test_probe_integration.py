import json
import socket
from unittest import mock

import pytest

from pyocd.core import exceptions
from pyocd.probe.debug_probe import DebugProbe
from pyocd.probe.pydapaccess.dap_access_cmsis_dap import READ, _Transfer
from pyocd.probe.shared_probe_proxy import SharedDebugProbeProxy
from pyocd.probe.stlink.stlink import STLink
from pyocd.probe.stlink_probe import StlinkProbe
from pyocd.probe.tcp_client_probe import TCPClientProbe
from pyocd.probe.tcp_probe_server import DebugProbeRequestHandler


class FakeSocket:
    def __init__(self, response=None):
        self.response = response
        self.closed = False
        self.connected = False
        self.writes = []

    def connect(self):
        self.connected = True

    def set_timeout(self, timeout):
        pass

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def readline(self):
        return self.response

    def close(self):
        self.closed = True


def make_tcp_probe(fake_socket, is_open=True):
    probe = TCPClientProbe.__new__(TCPClientProbe)
    DebugProbe.__init__(probe)
    probe._socket = fake_socket
    probe._is_open = is_open
    probe._request_id = 0
    probe._lock_count = 0
    probe._lock_count_lock = __import__('threading').RLock()
    return probe


def test_tcp_client_rejects_response_for_different_request():
    socket = FakeSocket(json.dumps({"id": 99, "status": 0}).encode() + b"\n")
    probe = make_tcp_probe(socket)

    with pytest.raises(exceptions.ProbeError, match="response ID"):
        probe._perform_request_without_raise('flush')


def test_tcp_client_open_failure_closes_socket():
    socket = FakeSocket()
    probe = make_tcp_probe(socket, is_open=False)
    probe._perform_request = mock.Mock(side_effect=exceptions.ProbeError("handshake failed"))

    with pytest.raises(exceptions.ProbeError):
        probe.open()

    assert socket.connected is True
    assert socket.closed is True
    assert probe.is_open is False


def test_tcp_client_close_failure_still_closes_socket():
    socket = FakeSocket()
    probe = make_tcp_probe(socket)
    probe._perform_request = mock.Mock(side_effect=exceptions.ProbeError("close failed"))

    with pytest.raises(exceptions.ProbeError):
        probe.close()

    assert socket.closed is True
    assert probe.is_open is False


class FakeSTLink:
    def __init__(self):
        self.protocols = []
        self.frequencies = []

    def enter_debug(self, protocol):
        self.protocols.append(protocol)

    def set_jtag_frequency(self, frequency):
        self.frequencies.append(('jtag', frequency))

    def set_swd_frequency(self, frequency):
        self.frequencies.append(('swd', frequency))


def make_stlink_probe(fake_link):
    probe = StlinkProbe.__new__(StlinkProbe)
    DebugProbe.__init__(probe)
    probe._link = fake_link
    probe._is_open = True
    probe._is_connected = False
    probe._protocol = None
    probe._memory_interfaces = {}
    return probe


def test_stlink_honours_jtag_protocol_and_clock():
    link = FakeSTLink()
    probe = make_stlink_probe(link)

    probe.connect(DebugProbe.Protocol.JTAG)
    probe.set_clock(2_000_000)

    assert link.protocols == [STLink.Protocol.JTAG]
    assert link.frequencies == [('jtag', 2_000_000)]
    assert probe.wire_protocol is DebugProbe.Protocol.JTAG


class FakeProbe:
    def __init__(self):
        self.open_calls = 0
        self.close_calls = 0
        self.connect_calls = 0
        self.disconnect_calls = 0
        self._wire_protocol = DebugProbe.Protocol.SWD

    @property
    def wire_protocol(self):
        return self._wire_protocol

    def open(self):
        self.open_calls += 1

    def close(self):
        self.close_calls += 1

    def connect(self, protocol):
        self.connect_calls += 1
        self._wire_protocol = protocol

    def disconnect(self):
        self.disconnect_calls += 1


def test_shared_probe_proxy_rejects_unbalanced_lifecycle_calls():
    proxy = SharedDebugProbeProxy(FakeProbe())

    with pytest.raises(exceptions.ProbeError):
        proxy.close()
    with pytest.raises(exceptions.ProbeError):
        proxy.disconnect()


def test_probe_request_handler_initializes_stream_files_in_setup():
    class Server:
        session = mock.Mock(log_tracebacks=False)
        probe = mock.Mock(spec=[
            'unique_id', 'session', 'open', 'close', 'lock', 'unlock', 'connect', 'disconnect',
            'swj_sequence', 'swd_sequence', 'jtag_sequence', 'set_clock', 'reset', 'assert_reset',
            'is_reset_asserted', 'flush', 'read_dp', 'write_dp', 'read_ap', 'write_ap',
            'read_ap_multiple', 'write_ap_multiple', 'get_memory_interface_for_ap', 'swo_start',
            'swo_stop', 'swo_read',
        ], unique_id="test-probe", session=None)

    left, right = socket.socketpair()
    handler = object.__new__(DebugProbeRequestHandler)
    handler.request = left
    handler.client_address = ('127.0.0.1', 12345)
    handler.server = Server()

    try:
        handler.setup()
        assert handler.rfile is not None
        assert handler.wfile is not None
    finally:
        handler.finish()
        right.close()


def test_deferred_transfer_error_is_raised_before_reading_transport():
    transfer = _Transfer(object(), 0, 1, READ, None)
    error = exceptions.TransferError("transfer failed")
    transfer.add_error(error)

    with pytest.raises(exceptions.TransferError, match="transfer failed"):
        transfer.get_result()


def test_cmsis_dap_block_response_decodes_little_endian_words():
    transfer = _Transfer(object(), 0, 3, READ, None)

    transfer.add_response(bytes.fromhex("78563412 efcdab90 00000000"))

    assert transfer.get_result() == [0x12345678, 0x90abcdef, 0]
