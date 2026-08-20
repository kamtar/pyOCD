# pyOCD debugger
# Copyright (c) 2006-2019,2025 Arm Limited
# Copyright (c) 2021-2022 Chris Reed
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import threading
import queue
import socket

CTRL_C = b'\x03'

LOG = logging.getLogger(__name__)

TRACE_ACK = LOG.getChild("trace.ack")
TRACE_ACK.setLevel(logging.CRITICAL)

TRACE_PACKETS = LOG.getChild("trace.packet")
TRACE_PACKETS.setLevel(logging.CRITICAL)

# When a packet_io thread sets the active index, this filter will
# prepend "packet_io<index>: " to log messages emitted on that thread.
class _PacketIOLogFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self._tls = threading.local()

    def set_client(self, index: int) -> None:
        self._tls.index = index

    def filter(self, record: logging.LogRecord) -> bool:
        idx = getattr(self._tls, 'index', None)
        if idx is not None:
            try:
                msg = record.getMessage()
            except Exception:
                msg = str(record.msg)
            record.msg = "Client %d: %s" % (idx, msg)
            record.args = ()
        return True

# Single shared filter instance for this module's logger.
_packet_io_log_filter = _PacketIOLogFilter()
LOG.addFilter(_packet_io_log_filter)

def checksum(data: bytes) -> bytes:
    return ("%02x" % (sum(data) % 256)).encode()

class ConnectionClosedException(Exception):
    """@brief Exception used to signal the GDB server connection closed."""
    pass

class GDBServerPacketIOThread(threading.Thread):
    """@brief Packet I/O thread.

    This class is a thread used by the GDBServer class to perform all RSP packet I/O. It
    handles verifying checksums, acking, and receiving Ctrl-C interrupts. There is a queue
    for received packets. The interface to this queue is the receive() method. The send()
    method writes outgoing packets to the socket immediately.
    """

    ## 100 ms timeout for socket and receive queue reads.
    RECEIVE_TIMEOUT = 0.1

    def __init__(self, socket, idx):
        super().__init__(daemon=True)
        self.name = "gdb-packet-io-%d" % idx
        self.idx = idx
        self._socket = socket
        self._receive_queue = queue.Queue()
        self._shutdown_event = threading.Event()
        self.interrupt_event = threading.Event()
        self.send_acks = True
        self._clear_send_acks = False
        self._buffer = b''
        self._expecting_ack = False
        self.drop_reply = False
        self._last_packet = b''
        self._closed = False
        self._write_lock = threading.Lock()
        self.start()

    def set_send_acks(self, ack):
        if ack:
            self.send_acks = True
        else:
            self._clear_send_acks = True

    def stop(self, timeout: float = 1.0):
        self._shutdown_event.set()
        self._closed = True
        self.join(timeout)

    def send(self, packet):
        if self._closed or not packet:
            return
        if not self.drop_reply:
            self._last_packet = packet
            self._write_packet(packet)
        else:
            self.drop_reply = False
            LOG.debug("Packet IO is dropping replay: %s", packet)

    def receive(self, block=True):
        if self._closed:
            raise ConnectionClosedException()
        while True:
            try:
                # If block is false, we'll get an Empty exception immediately if there
                # are no packets in the queue. Same if block is true and it times out
                # waiting on an empty queue.
                return self._receive_queue.get(block, self.RECEIVE_TIMEOUT)
            except queue.Empty:
                # Only exit the loop if block is false or connection closed.
                if not block:
                    return None
                if self._closed:
                    raise ConnectionClosedException()

    def run(self):
        # Set the log filter to include the packet_io index in messages from this thread.
        _packet_io_log_filter.set_client(self.idx)

        LOG.debug("Packet IO thread started")

        self._socket.set_timeout(self.RECEIVE_TIMEOUT)

        while not self._shutdown_event.is_set():
            try:
                data = self._socket.read()

                # Handle closed connection
                if len(data) == 0:
                    LOG.debug("Packet IO connection closed by remote")
                    self._closed = True
                    break

                TRACE_PACKETS.debug('-->>>> GDB read %d bytes: %s', len(data), data)

                self._buffer += data
            except (ConnectionAbortedError, ConnectionResetError) as err:
                LOG.warning("Packet IO connection unexpectedly closed during receive: (%s)", err)
                self._closed = True
                break
            except socket.timeout:
                # Ignore timeouts.
                pass
            except OSError as err:
                LOG.debug("Packet IO connection error during receive: %s", err)
                self._closed = True
                break

            if self._shutdown_event.is_set():
                break

            self._process_data()

        LOG.debug("Packet IO thread exited")

    def _write_packet(self, packet):
        TRACE_PACKETS.debug('--<<<< GDB send %d bytes: %s', len(packet), packet)

        if not self._write_raw(packet):
            return False

        if self.send_acks:
            self._expecting_ack = True
        return True

    def _write_raw(self, data):
        """Write all of *data* while serializing writes from both packet paths."""
        with self._write_lock:
            remaining = len(data)
            while remaining:
                try:
                    written = self._socket.write(data)
                except OSError as err:
                    LOG.warning("Packet IO connection unexpectedly closed during send (%s)", err)
                    self._closed = True
                    return False
                if written is None or written <= 0:
                    LOG.warning("Packet IO socket returned zero bytes during send")
                    self._closed = True
                    return False
                remaining -= written
                if remaining:
                    data = data[written:]
        return True

    def _check_expected_ack(self):
        # Handle expected ack.
        c = self._buffer[0:1]
        if c in (b'+', b'-'):
            self._buffer = self._buffer[1:]
            TRACE_ACK.debug('Packet IO received ack: %s', c)
            if c == b'-':
                # Handle nack from gdb
                self._write_packet(self._last_packet)
                return

            # Handle disabling of acks.
            if self._clear_send_acks:
                self.send_acks = False
                self._clear_send_acks = False
        else:
            LOG.debug("Packet IO expected ack/nack but received '%s'", c)

    def _process_data(self):
        """@brief Process all incoming data until there are no more complete packets."""
        while len(self._buffer):
            if self._expecting_ack:
                self._expecting_ack = False
                self._check_expected_ack()

            # Check for a ctrl-c.
            if len(self._buffer) and self._buffer[0:1] == CTRL_C:
                self.interrupt_event.set()
                self._buffer = self._buffer[1:]

            # Look for a complete packet and extract it from the buffer. The '#'
            # delimiter must be searched after the '$' start delimiter; otherwise
            # junk or a '#' in a partial packet can desynchronise the stream.
            pkt_begin = self._buffer.find(b"$")
            if pkt_begin < 0:
                # Discard non-packet data so an invalid peer cannot grow the
                # receive buffer without bound.
                self._buffer = b''
                break

            if pkt_begin:
                self._buffer = self._buffer[pkt_begin:]

            pkt_end = self._buffer.find(b"#", 1)
            if pkt_end < 0 or len(self._buffer) < pkt_end + 3:
                # No complete packet received yet.
                break

            packet_end = pkt_end + 3
            pkt = self._buffer[:packet_end]
            self._buffer = self._buffer[packet_end:]
            self._handling_incoming_packet(pkt)

    def _handling_incoming_packet(self, packet):
        # Compute checksum
        data, delimiter, cksum = packet[1:].partition(b'#')
        good_format = (delimiter == b'#') and (len(cksum) == 2)
        computedCksum = checksum(data)
        goodPacket = good_format and (computedCksum.lower() == cksum.lower())

        if self.send_acks:
            ack = b'+' if goodPacket else b'-'
            self._write_raw(ack)
            TRACE_ACK.debug("Packet IO sending ack: %s", ack)

        if goodPacket:
            self._receive_queue.put(packet)
