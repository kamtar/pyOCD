import queue

from pyocd.gdbserver.packet_io import GDBServerPacketIOThread, checksum


class FakeSocket:
    def __init__(self, write_result=None):
        self.writes = []
        self.write_result = write_result

    def write(self, data):
        self.writes.append(data)
        return len(data) if self.write_result is None else self.write_result


def make_packet_io(fake_socket):
    packet_io = GDBServerPacketIOThread.__new__(GDBServerPacketIOThread)
    packet_io._socket = fake_socket
    packet_io._receive_queue = queue.Queue()
    packet_io._buffer = b''
    packet_io._expecting_ack = False
    packet_io.send_acks = True
    packet_io._clear_send_acks = False
    packet_io._last_packet = b''
    packet_io._closed = False
    packet_io._write_lock = __import__('threading').Lock()
    return packet_io


def test_packet_parser_resynchronizes_after_junk_with_hash():
    fake_socket = FakeSocket()
    packet_io = make_packet_io(fake_socket)
    payload = b"qSupported"
    packet = b"$" + payload + b"#" + checksum(payload)

    packet_io._buffer = b"stale#data" + packet
    packet_io._process_data()

    assert packet_io._receive_queue.get_nowait() == packet
    assert fake_socket.writes == [b'+']


def test_packet_parser_rejects_malformed_packet_and_continues():
    fake_socket = FakeSocket()
    packet_io = make_packet_io(fake_socket)
    payload = b"qAttached"
    packet = b"$" + payload + b"#" + checksum(payload)

    # The extra '#' used to make split() raise and terminate the packet thread.
    packet_io._buffer = b"$bad#x#" + packet
    packet_io._process_data()

    assert fake_socket.writes == [b'-', b'+']
    assert packet_io._receive_queue.get_nowait() == packet


def test_packet_write_zero_progress_closes_connection():
    packet_io = make_packet_io(FakeSocket(write_result=0))

    assert packet_io._write_packet(b"$x#00") is False
    assert packet_io._closed is True
