import socket
from types import SimpleNamespace

from pyocd.utility.columns import ColumnFormatter
from pyocd.utility.mask import bfxw
from pyocd.utility.rtt_server import RTTChanTCPWorker
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
