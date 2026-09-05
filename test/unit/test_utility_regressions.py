import socket
from types import SimpleNamespace

import pytest

from pyocd.utility.columns import ColumnFormatter
from pyocd.utility.mask import bfxw
from pyocd.utility.rtt_server import RTTChanTCPWorker, RTTServer, _RTTDataQueue
from pyocd.utility.sockets import ClientSocket, ConnectedSocket
from pyocd.utility.stdio import StdioFile


def test_column_formatter_handles_generators_and_narrow_terminals():
    formatter = ColumnFormatter(maxwidth=1)
    formatter.add_items((item for item in [("name", "value")]))

    assert "name" in formatter.format()
    assert "value" in formatter.format()


def test_column_formatter_empty_is_empty():
    assert ColumnFormatter(maxwidth=80).format() == ""


def test_bfxw_width_is_inclusive_of_exactly_width_bits():
    assert bfxw(0b1111, 0, 1) == 1
    assert bfxw(0b111100, 2, 2) == 0b11


def test_client_socket_readline_returns_at_eof_and_closes_after_peer_close():
    peer, client_socket = socket.socketpair()
    client = ClientSocket("unused", 0)
    client._socket = client_socket
    try:
        peer.sendall(b"partial")
        peer.close()

        assert client.readline() == b"partial"
        assert client.readline() == b""
        client.close()
        assert client._socket is None
    finally:
        peer.close()
        client.close()


def test_client_socket_discards_buffer_on_reconnect(monkeypatch):
    old_peer, old_socket = socket.socketpair()
    new_peer, new_socket = socket.socketpair()
    client = ClientSocket("unused", 0)
    client._socket = old_socket
    old_peer.sendall(b"stale\nold\n")
    try:
        assert client.readline() == b"stale\n"

        monkeypatch.setattr(socket, "create_connection", lambda address, timeout: new_socket)
        client.connect()
        new_peer.sendall(b"fresh\n")

        assert client.readline() == b"fresh\n"
    finally:
        old_peer.close()
        new_peer.close()
        client.close()


def test_client_socket_readline_scans_only_new_bytes():
    class TrackingBuffer(bytearray):
        def __init__(self):
            super().__init__()
            self.starts = []

        def find(self, sub, start=0, end=None):
            self.starts.append(start)
            return super().find(sub, start) if end is None else super().find(sub, start, end)

    peer, client_socket = socket.socketpair()
    client = ClientSocket("unused", 0, packet_size=7)
    client._socket = client_socket
    client._buffer = TrackingBuffer()
    try:
        peer.sendall(b"x" * 1000 + b"\nsecond\n")

        assert client.readline() == b"x" * 1000 + b"\n"
        assert client.readline() == b"second\n"
        assert client._buffer.starts[0] == 0
        assert any(start > 0 for start in client._buffer.starts)
        assert client._buffer.starts[-1] == 0
    finally:
        peer.close()
        client.close()


class _RttUpSource:
    def __init__(self, data=b"abcd"):
        self.data = data
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.data


class _RttDownSink:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))
        return 0


class _RttSlowWorker:
    def __init__(self):
        self.up_writes = []

    def write_up_data(self, data):
        self.up_writes.append(bytes(data))
        return 0

    def get_down_data(self):
        return b"wxyz"

    def close(self):
        pass


class _RttPartialWorker(_RttSlowWorker):
    def write_up_data(self, data):
        self.up_writes.append(bytes(data[:1]))
        return min(1, len(data))


def test_rtt_server_queues_are_bounded_and_keep_order():
    up_source = _RttUpSource()
    down_sink = _RttDownSink()
    worker = _RttSlowWorker()
    server = object.__new__(RTTServer)
    server.control_block = SimpleNamespace(
        up_channels=[up_source], down_channels=[down_sink])
    server.up_buffers = [_RTTDataQueue(8)]
    server.down_buffers = [_RTTDataQueue(8)]

    for _ in range(1000):
        server._channel_handler(0, worker)

    assert len(server.up_buffers[0]) <= 8
    assert len(server.down_buffers[0]) <= 8
    assert up_source.reads < 1000
    assert worker.up_writes[0] == b"abcd"
    assert down_sink.writes[0] == b"wxyz"

    # The first complete chunks are retained when the destination is stuck.
    assert b"".join(server.up_buffers[0]._chunks) == b"abcdabcd"
    assert b"".join(server.down_buffers[0]._chunks) == b"wxyzwxyz"


def test_rtt_server_partial_writes_preserve_byte_order():
    up_source = _RttUpSource(b"")
    worker = _RttPartialWorker()
    server = object.__new__(RTTServer)
    server.control_block = SimpleNamespace(up_channels=[up_source], down_channels=[])
    server.up_buffers = [_RTTDataQueue(8)]
    server.down_buffers = [_RTTDataQueue(8)]
    server.up_buffers[0].append(b"0123")
    server.up_buffers[0].append(b"4567")

    for _ in range(8):
        server._channel_handler(0, worker)

    assert len(server.up_buffers[0]) == 0
    assert b"".join(worker.up_writes) == b"01234567"


def test_rtt_tcp_worker_clears_client_after_terminal_send_error():
    closed = []

    class DeadClient:
        def send(self, data):
            raise BrokenPipeError()

        def close(self):
            closed.append(True)

    worker = object.__new__(RTTChanTCPWorker)
    worker.server = None
    worker.client = DeadClient()

    with pytest.raises(BrokenPipeError):
        worker.write_up_data(b"data")

    assert worker.client is None
    assert closed == [True]


def test_rtt_tcp_worker_accepts_replacement_after_upload_disconnect(monkeypatch):
    closed = []
    sent = []

    class DeadClient:
        def send(self, data):
            raise BrokenPipeError()

        def close(self):
            closed.append("dead")

    class ReplacementClient:
        def setblocking(self, blocking):
            pass

        def send(self, data):
            sent.append(bytes(data))
            return len(data)

    replacement = ReplacementClient()

    class Server:
        def accept(self):
            return replacement, None

    class Selector:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            pass

        def register(self, fileobj, events, data):
            self.fileobj = fileobj

        def select(self, timeout):
            return [(SimpleNamespace(fileobj=self.fileobj), None)]

    monkeypatch.setattr("pyocd.utility.rtt_server.selectors.DefaultSelector", Selector)
    worker = object.__new__(RTTChanTCPWorker)
    worker.server = Server()
    worker.client = DeadClient()

    with pytest.raises(BrokenPipeError):
        worker.write_up_data(b"old")
    assert worker.client is None

    assert worker.write_up_data(b"new") == 3
    assert sent == [b"new"]
    assert closed == ["dead"]


def test_connected_socket_formats_non_ip_peer_names():
    sock = SimpleNamespace(
        getpeername=lambda: ("probe-host", 1234),
    )
    assert ConnectedSocket(sock, 16).get_remote_address() == "probe-host:1234"


def test_stdio_file_missing_core_input_is_disabled(tmp_path):
    output = tmp_path / "stdio.out"

    class Options:
        values = {
            "stdio_file_out": str(output),
            "stdio_file_in": [str(tmp_path / "core0.in")],
        }

        def is_set(self, name):
            return name in self.values

        def get(self, name):
            return self.values[name]

    session = SimpleNamespace(
        options=Options(),
        board=SimpleNamespace(
            target=SimpleNamespace(cores={0: object(), 1: object()}),
            target_type="test-target",
        ),
    )
    stdio = StdioFile(session, core=1)
    try:
        assert stdio.read(16) == b""
    finally:
        stdio.shutdown()


def test_rtt_tcp_worker_closes_poll_selectors(monkeypatch):
    closed = []

    class Selector:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.close()

        def register(self, fileobj, events, data):
            pass

        def select(self, timeout):
            return []

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        "pyocd.utility.rtt_server.selectors.DefaultSelector", Selector)

    worker = object.__new__(RTTChanTCPWorker)
    worker.server = object()
    worker.client = None
    worker._check_for_new_client()

    worker.server = None
    worker.client = object()
    assert worker.get_down_data() == b""

    assert closed == [True, True]
