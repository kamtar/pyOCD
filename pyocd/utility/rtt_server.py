# pyOCD debugger
# Copyright (c) 2022 Samuel Dewan
# Copyright (c) 2026 Arm Limited
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

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
import selectors
import socket
from typing import Deque, Optional, Sequence, Callable, IO
import os
from time import sleep
from pathlib import Path
import logging

from ..core.soc_target import SoCTarget
from ..core import exceptions
from ..debug.rtt import RTTControlBlock, RTTUpChannel, RTTDownChannel
from ..utility.stdio import StdioHandler

LOG = logging.getLogger(__name__)


class _RTTDataQueue:
    """Bounded FIFO of byte chunks used by an RTT channel."""

    def __init__(self, max_size: int):
        self._max_size = max_size
        self._size = 0
        self._chunks: Deque[bytes] = deque()
        self._overflow_reported = False

    def __bool__(self) -> bool:
        return bool(self._chunks)

    def __len__(self) -> int:
        return self._size

    @property
    def max_size(self) -> int:
        return self._max_size

    def append(self, data: bytes) -> int:
        """Append as much data as fits and return the number of bytes kept.

        If the queue is full, new data is dropped. This explicit overflow
        policy preserves existing data and its order. The channel handler also
        stops reading while a queue is full, providing backpressure in the
        usual case.
        """
        if not data:
            return 0

        bytes_to_keep = min(len(data), self._max_size - self._size)
        if bytes_to_keep <= 0:
            return 0

        self._chunks.append(bytes(data[:bytes_to_keep]))
        self._size += bytes_to_keep
        return bytes_to_keep

    def report_overflow(self) -> bool:
        """Return true once per backlog episode when data is discarded."""
        if self._overflow_reported:
            return False
        self._overflow_reported = True
        return True

    def peek(self) -> bytes:
        return self._chunks[0]

    def consume(self, count: int) -> None:
        if count < 0 or count > self._size:
            raise ValueError(f"Invalid RTT queue consume count: {count}")
        while count:
            chunk = self._chunks[0]
            if count >= len(chunk):
                self._chunks.popleft()
                count -= len(chunk)
                self._size -= len(chunk)
            else:
                self._chunks[0] = chunk[count:]
                self._size -= count
                count = 0
        if self._size == 0:
            self._overflow_reported = False


class RTTChanWorker(ABC):
    """@brief Source and sink for data to be transferred over RTT. """

    @abstractmethod
    def write_up_data(self, data: bytes) -> int:
        """@brief Write data that has been received from an up channel to the
                  correct destination.

        @param data The data to be written.
        @return The number of bytes that were successfully written.
        """
        pass

    @abstractmethod
    def get_down_data(self) -> bytes:
        """@brief Get data that should be written to a down channel if there is
                  any.

        @return Data to be written to down channel.
        """
        pass

    @abstractmethod
    def close(self):
        """@brief Cleanup channel worker and close any file descriptors."""
        pass

class RTTChanTCPWorker(RTTChanWorker):
    """@brief Implementation of channel worker that forwards RTT data via a TCP
              socket. """

    port: int

    def __init__(self, port: int, listen: bool = True):
        """
        @param port The port to connect to or to listen for connects on.
        @param listen If true a server will be started to accept one connection
                      at a time on the given port. If false a connection will be
                      made as a TCP client to a server running on the given
                      port on localhost.
        """
        if listen:
            self.server = socket.socket()
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind(('localhost', port))
            self.server.listen(1)
            self.server.setblocking(False)
            self.client = None
        else:
            self.server = None
            self.client = socket.create_connection(('localhost', port), timeout = 1.0)
            self.client.setblocking(False)

        self.port = port

    def _check_for_new_client(self):
        if self.server is None:
            return

        with selectors.DefaultSelector() as sel:
            sel.register(self.server, selectors.EVENT_READ, None)
            events = sel.select(timeout = 0)
        for key, _ in events:
            if key.fileobj == self.server:
                try:
                    self.client, _ = self.server.accept()
                    self.client.setblocking(False)
                except BlockingIOError:
                    # The connection may have disappeared between select()
                    # and accept(). Try again on a later poll.
                    self.client = None

    def _close_client(self):
        client = self.client
        self.client = None
        if client is not None:
            try:
                client.close()
            except OSError:
                pass

    def _send_client_data(self, data: bytes) -> int:
        try:
            bytes_written = self.client.send(data)
        except BlockingIOError:
            # The socket is still connected, but its send buffer is full.
            return 0
        except OSError:
            self._close_client()
            raise
        if data and bytes_written == 0:
            # A zero-byte send for non-empty data is a terminal condition.
            self._close_client()
        return bytes_written

    def write_up_data(self, data: bytes):
        if self.client is None:
            self._check_for_new_client()
            if self.client is None:
                return 0

        return self._send_client_data(data)

    def get_down_data(self):
        if self.client is None:
            self._check_for_new_client()
            if self.client is None:
                return b''

        try:
            with selectors.DefaultSelector() as sel:
                sel.register(self.client, selectors.EVENT_READ, None)
                events = sel.select(timeout = 0)
        except BlockingIOError:
            return b''
        except OSError:
            self._close_client()
            raise
        for key, _ in events:
            if key.fileobj == self.client:
                try:
                    data = self.client.recv(4096)
                except BlockingIOError:
                    # Readiness can race with another consumer.
                    return b''
                except OSError:
                    self._close_client()
                    raise
                if not data:
                    # client socket closed at other end
                    self._close_client()
                return data

        return bytes()

    def close(self):
        if self.server is not None:
            self.server.close()
            self.server = None
        self._close_client()

class RTTChanFileWorker(RTTChanWorker):
    """@brief Implementation of channel worker that writes data from RTT channel
              to a file and optionally reads data from a file into an RTT
              channel. """
    _f_out: Optional[IO[bytes]]
    _f_in: Optional[IO[bytes]]
    _f_out_path: Optional[str]
    _f_in_path: Optional[str]

    def __init__(self, channel: int, file_out: Optional[str] = None, file_in: Optional[str] = None):
        """
        @param file_out The file to write RTT channel data to.
        @param file_in The file to read data from into the RTT channel. If None, no data will be read.
        """
        self._f_out = file_out
        self._f_in = file_in

        if file_out is not None:
            try:
                self._f_out = open(file_out, 'wb')
            except OSError as e:
                raise OSError(f"Failed to open RTT output file {file_out}: {e}")

        if file_in is not None:
            try:
                self._f_in = open(file_in, 'rb')
            except OSError as e:
                if self._f_out is not None:
                    self._f_out.close()
                    self._f_out = None
                raise OSError(f"Failed to open RTT input file {file_in}: {e}")


    def write_up_data(self, data: bytes):
        if self._f_out is None or not data:
            return 0
        return self._f_out.write(data)

    def get_down_data(self):
        if self._f_in is None:
            return b''
        return self._f_in.read(4096)

    def close(self):
        if self._f_out is not None:
            self._f_out.close()
        if self._f_in is not None:
            self._f_in.close()

class RTTChanSysViewFileWorker(RTTChanWorker):
    """@brief Implementation of channel worker that writes data from RTT channel
              to a SystemView file and handles START and STOP commands. """
    _START_CMD = b"\x01"
    _STOP_CMD  = b"\x02"
    _START_SEQ = b"\x00" * 10

    def __init__(self, rtt_server: RTTServer, channel: int, file_out: str, auto_start: bool = True, auto_stop: bool = True):
        self._rtt_server = rtt_server
        self._rtt_channel = channel
        self._auto_start = auto_start
        self._auto_stop = auto_stop

        self._started = not auto_start
        self._up_buffer = b""
        self._f_out = None
        self._f_out_path = None

        # Check if the folder exists for output file
        dir_out = os.path.dirname(file_out)
        if dir_out and not os.path.exists(dir_out):
            f_name_out = os.path.basename(file_out)
            raise FileNotFoundError(
                f"Output directory '{dir_out}' for RTT channel {self._rtt_channel} (file '{f_name_out}') does not exist."
            )
        try:
            self._f_out = open(file_out, 'wb')
            self._f_out_path = file_out
        except OSError as e:
            raise OSError(f"Failed to open SystemView output file {file_out}: {e}")

    def write_up_data(self, data: bytes):
        if self._f_out is None:
            return 0

        # If not started: search for start sequence; drop everything before it.
        if not self._started:
            self._up_buffer += data
            pos = self._up_buffer.find(self._START_SEQ)
            if pos < 0:
                # Keep last few bytes in case start sequence is split across writes, but drop the rest
                seq_len = len(self._START_SEQ)
                if len(self._up_buffer) > seq_len:
                    self._up_buffer = self._up_buffer[-seq_len:]
                return len(data)
            else:
                self._started = True
                to_write = self._up_buffer[pos:]
                self._up_buffer = b""
                self._f_out.write(to_write)
                return len(data)

        # Started (or auto_start disabled): write everything
        self._f_out.write(data)
        return len(data)

    def get_down_data(self):
        if not self._started:
            if self._rtt_channel >= len(self._rtt_server.control_block.down_channels):
                return b""
            down_chan: RTTDownChannel = self._rtt_server.control_block.down_channels[self._rtt_channel]
            if down_chan.bytes_free == down_chan.size:
                # Channel is empty, can start
                LOG.debug("SystemView START command for channel %d sent", self._rtt_channel)
                down_chan.write(self._START_CMD)
        return b""

    def close(self):
        if self._auto_stop:
            if self._rtt_channel >= len(self._rtt_server.control_block.down_channels):
                LOG.error("SystemView worker for channel %d does not have a configured RTT channel; ignoring stop request", self._rtt_channel)
            else:
                down_chan: RTTDownChannel = self._rtt_server.control_block.down_channels[self._rtt_channel]
                LOG.debug("SystemView STOP command for channel %d sent", self._rtt_channel)
                down_chan.write(self._STOP_CMD)
        if self._f_out is not None:
            self._f_out.close()

class RTTChanSysViewTCPWorker(RTTChanTCPWorker):
    """@brief Implementation of channel worker that handles SystemView Hello messages and
              forwards RTT data via a TCP socket. """

    _HELLO_MSG = b"SEGGER SystemView"

    hello_received: bool

    def __init__(self, port: int, listen: bool = True):
        super().__init__(port, listen)
        self.hello_received = False

    def get_down_data(self):
        data = super().get_down_data()

        if self.client is None:
            self.hello_received = False
            return b''

        if not data:
            return b''

        if not self.hello_received:
            # First message from SystemView client should be 32 byte hello message starting with _HELLO_MSG
            if len(data) == 32 and data.startswith(self._HELLO_MSG):
                self.hello_received = True
                LOG.debug("Received hello message from SystemView client on port %d; connection established", self.port)
                # Return hello response
                response = self._HELLO_MSG
                response += b"\x00" * (32 - len(response))
                try:
                    self._send_client_data(response)
                except OSError:
                    self._close_client()
            else:
                LOG.debug("Received non-hello message from SystemView client before hello message; ignoring")
            return b''

        return data[1:data[0] + 1]

class RTTChanStdioWorker(RTTChanWorker):
    """@brief Implementation of channel worker that forwards RTT data via a STDIO"""

    _stdio: StdioHandler

    def __init__(self, channel: int, stdio: StdioHandler):
        """
        @param stdio The STDIO handler to use for RTT channel data.
        """
        self._stdio = stdio

    def write_up_data(self, data: bytes):
        if self._stdio is None:
            return 0
        return self._stdio.write(data)

    def get_down_data(self):
        if self._stdio is None:
            return b''
        return self._stdio.read(4096)

    def close(self):
        pass
        # if self._stdio is not None:
        #     self._stdio.shutdown()

class RTTServer:
    """@brief Keeps track of polling for multiple active RTT channels and the
              sources and sinks of data for each channel. """
    control_block: RTTControlBlock
    workers: Optional[Sequence[Optional[RTTChanWorker]]]
    up_buffers: Optional[Sequence[_RTTDataQueue]]
    down_buffers: Optional[Sequence[_RTTDataQueue]]

    # Keep each directional queue bounded. A chunk queue avoids repeatedly
    # copying the complete backlog when data is only partially consumed.
    RTT_BUFFER_MAX_SIZE = 1024 * 1024

    def __init__(self, target: SoCTarget, address: int, size: int,
                 control_block_id: bytes):
        """
        @param target The target with which RTT communication is desired.
        @param address Base address for control block search range.
        @param size Control block search range. If 0 the control block will be
                    expected to be located at the provided address.
        @param control_block_id The control block ID string to search for. Must
                                be at most 16 bytes long.  Will be padded with
                                zeroes if less than 16 bytes.
        """
        self.control_block = RTTControlBlock.from_target(target, address = address,
                                    size = size, control_block_id = control_block_id)

        self.workers = None
        self.up_buffers = None
        self.down_buffers = None

    def _channel_handler(self, ch_idx: int, worker: RTTChanWorker):
        if ch_idx < len(self.control_block.up_channels):
            up_buffer = self.up_buffers[ch_idx]
            if len(up_buffer) < up_buffer.max_size:
                try:
                    # Do not read from the target while the destination queue
                    # is full. If a single source read is larger than the
                    # remaining capacity, the queue's drop-newest policy
                    # keeps the backlog bounded.
                    data = self.control_block.up_channels[ch_idx].read()
                    bytes_kept = up_buffer.append(data)
                    if bytes_kept < len(data) and up_buffer.report_overflow():
                        LOG.warning("RTT up channel %d queue full; dropped %d bytes",
                                    ch_idx, len(data) - bytes_kept)
                except (exceptions.TransferError, exceptions.RTTError) as e:
                    LOG.error("Error reading RTT up channel %d: %s", ch_idx, e)
            try:
                # Write to worker
                if up_buffer:
                    bytes_written = worker.write_up_data(up_buffer.peek())
                    up_buffer.consume(bytes_written)
            except Exception as e:
                LOG.error("Error writing to RTT channel worker %d: %s", ch_idx, e)

        if ch_idx < len(self.control_block.down_channels):
            down_buffer = self.down_buffers[ch_idx]
            if len(down_buffer) < down_buffer.max_size:
                try:
                    # Read from worker only while there is room in the
                    # bounded queue.
                    data = worker.get_down_data()
                    bytes_kept = down_buffer.append(data)
                    if bytes_kept < len(data) and down_buffer.report_overflow():
                        LOG.warning("RTT down channel %d queue full; dropped %d bytes",
                                    ch_idx, len(data) - bytes_kept)
                except Exception as e:
                    LOG.error("Error reading from RTT channel %d: %s", ch_idx, e)
            try:
                # Write to down channel
                if down_buffer:
                    bytes_out = self.control_block.down_channels[ch_idx].write(down_buffer.peek())
                    down_buffer.consume(bytes_out)
            except (exceptions.TransferError, exceptions.RTTError) as e:
                LOG.error("Error writing RTT down channel %d: %s", ch_idx, e)

    def poll(self):
        """@brief Reads from and writes to active RTT channels. """
        if not self.running:
            # not yet started
            return

        for i, worker in enumerate(self.workers):
            if worker is None:
                continue
            self._channel_handler(i, worker)

    def start(self):
        """@brief Find and parse RTT control block. """
        self.control_block.start()

        num_up_chans: int = len(self.control_block.up_channels)
        num_down_chans: int = len(self.control_block.down_channels)
        num_chans: int = max(num_up_chans, num_down_chans)

        self.workers = [None] * num_chans
        self.up_buffers = [_RTTDataQueue(self.RTT_BUFFER_MAX_SIZE) for _ in range(num_chans)]
        self.down_buffers = [_RTTDataQueue(self.RTT_BUFFER_MAX_SIZE) for _ in range(num_chans)]

    def stop(self):
        """@brief Close all RTT workers. """
        if not self.running:
            return

        try:
            for i, worker in enumerate(self.workers):
                if worker is not None:
                    try:
                        worker.close()
                    except Exception:
                        LOG.exception("Error closing RTT channel worker %d", i)
        finally:
            self.workers = None
            self.up_buffers = None
            self.down_buffers = None

    @property
    def running(self):
        """@brief True if RTT is started. """
        return self.workers is not None

    def is_channel_idx_valid(self, channel: int) -> bool:
        """Return True if channel is a valid index for current workers."""
        if not isinstance(channel, int):
            return False
        if not self.running:
            return False
        return 0 <= channel < len(self.workers)

    def is_channel_configured(self, channel: int) -> bool:
        """Return True if channel is a valid index for current workers and has a worker configured."""
        if not self.is_channel_idx_valid(channel):
            return False
        return self.workers[channel] is not None

    def add_channel_worker(self, channel: int, worker: Callable[[], RTTChanWorker]):
        self.workers[channel] = worker()

    def remove_channel_worker(self, channel: int):
        if not self.is_channel_idx_valid(channel):
            raise exceptions.RTTError(f"Invalid channel index {channel}")
        worker = self.workers[channel]
        if worker is not None:
            worker.close()
            self.workers[channel] = None

    def add_server(self, port: int, channel: int):
        """@brief Start a new TCP server to communicate with a given RTT channel.

        @param port The port on which the server should listen for new connections.
        @param channel The RTT channel which should be exposed over TCP.
        """
        if not self.running:
            raise exceptions.RTTError("RTT is not yet started")
        elif self.workers[channel] is not None:
            raise exceptions.RTTError(f"RTT is already started for channel {channel}")
        self.add_channel_worker(channel, lambda: RTTChanTCPWorker(port, listen = True))

    def stop_server(self, channel: Optional[int] = None, port: Optional[int] = None):
        """@brief Stop a TCP server.

        @param port The port of the server to be stopped.
        """

        if not self.running:
            return
        if channel is not None:
            return self.remove_channel_worker(channel)

        # Fallback: if channel not specified, search for server with given port and stop it
        for i, worker in enumerate(self.workers):
            if isinstance(worker, RTTChanTCPWorker):
                if worker.port == port:
                    worker.close()
                    self.workers[i] = None
