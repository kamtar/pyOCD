"""Small, synchronous GDB/MI2 client used by the web debugger.

The web controller is already serialised, so this class deliberately exposes a
blocking API while reader threads continuously drain GDB's output streams.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import queue
import re
import shutil
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, Optional


class GdbMiError(RuntimeError):
    """Raised for GDB process, protocol, or command failures."""


@dataclass
class MiRecord:
    token: Optional[int]
    prefix: str
    cls: str
    payload: Any


class _MiValueParser:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def parse_results(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        while self.pos < len(self.text):
            key = self._identifier()
            self._expect("=")
            value = self._value()
            if key in result:
                old = result[key]
                result[key] = old + [value] if isinstance(old, list) else [old, value]
            else:
                result[key] = value
            if not self._take(","):
                break
        return result

    def _value(self) -> Any:
        if self.pos >= len(self.text):
            return ""
        ch = self.text[self.pos]
        if ch == '"':
            try:
                value, end = json.JSONDecoder().raw_decode(self.text[self.pos:])
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise GdbMiError(f"Invalid GDB/MI string: {exc}") from exc
            self.pos += end
            return value
        if ch == "{":
            self.pos += 1
            value = self.parse_results() if not self._peek("}") else {}
            self._expect("}")
            return value
        if ch == "[":
            self.pos += 1
            items = []
            while not self._peek("]"):
                start = self.pos
                try:
                    key = self._identifier()
                    is_result = self._take("=")
                except GdbMiError:
                    is_result = False
                if is_result:
                    items.append({key: self._value()})
                else:
                    self.pos = start
                    items.append(self._value())
                if not self._take(","):
                    break
            self._expect("]")
            return items
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in ",}]":
            self.pos += 1
        return self.text[start:self.pos]

    def _identifier(self) -> str:
        match = re.match(r"[A-Za-z0-9_\-]+", self.text[self.pos:])
        if not match:
            raise GdbMiError(f"Expected GDB/MI identifier at {self.pos}")
        self.pos += len(match.group(0))
        return match.group(0)

    def _peek(self, value: str) -> bool:
        return self.text.startswith(value, self.pos)

    def _take(self, value: str) -> bool:
        if self._peek(value):
            self.pos += len(value)
            return True
        return False

    def _expect(self, value: str) -> None:
        if not self._take(value):
            raise GdbMiError(f"Expected {value!r} at {self.pos}")


def parse_mi_record(line: str) -> Optional[MiRecord]:
    """Parse one MI output record; prompts and blank lines are ignored."""
    line = line.strip()
    if not line or line == "(gdb)":
        return None
    match = re.match(r"^(\d+)?([\^*+=~@&])(.*)$", line)
    if not match:
        return MiRecord(None, "?", "output", line)
    token = int(match.group(1)) if match.group(1) else None
    prefix, body = match.group(2), match.group(3)
    if prefix in "~@&":
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = body
        return MiRecord(token, prefix, "stream", payload)
    cls, comma, rest = body.partition(",")
    payload = _MiValueParser(rest).parse_results() if comma else {}
    return MiRecord(token, prefix, cls, payload)


def quote_mi(value: str) -> str:
    """Return a GDB/MI-compatible C string."""
    return json.dumps(value)


class GdbMiClient:
    """Own a GDB subprocess and exchange tokenised MI2 commands with it."""

    DEFAULT_EXECUTABLES = ("arm-none-eabi-gdb", "gdb-multiarch", "gdb")

    @classmethod
    def find_executable(cls, configured: Optional[str] = None) -> str:
        if configured:
            found = shutil.which(configured)
            if found:
                return found
            raise GdbMiError(f"GDB executable was not found: {configured}")
        for name in cls.DEFAULT_EXECUTABLES:
            found = shutil.which(name)
            if found:
                return found
        raise GdbMiError(
            "No GDB executable found; install arm-none-eabi-gdb or use --gdb-executable")

    def __init__(self, executable: Optional[str] = None,
                 event_handler: Optional[Callable[[MiRecord], None]] = None):
        self.executable = self.find_executable(executable)
        self._event_handler = event_handler
        self._process: Optional[subprocess.Popen[str]] = None
        self._pending: Dict[int, queue.Queue[MiRecord]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._token = 0
        self.stderr: list[str] = []
        self.stream: list[str] = []

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        if self.is_alive:
            return
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._process = subprocess.Popen(
            [self.executable, "--interpreter=mi2", "--quiet", "--nx"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=creationflags)
        assert self._process.stdout and self._process.stderr
        threading.Thread(target=self._read_stdout, daemon=True,
                         name="pyocd-web-gdb-mi").start()
        threading.Thread(target=self._read_stderr, daemon=True,
                         name="pyocd-web-gdb-stderr").start()
        self.command("-gdb-set pagination off")
        self.command("-gdb-set confirm off")
        self.command("-gdb-set print elements 200")
        # Without MI async mode, execution commands do not return until the
        # target stops, which would block an HTTP worker for the entire run.
        self.command("-gdb-set mi-async on")

    def command(self, command: str, timeout: float = 10.0) -> Dict[str, Any]:
        process = self._process
        if not process or process.poll() is not None or not process.stdin:
            detail = self.stderr[-1] if self.stderr else "GDB is not running"
            raise GdbMiError(detail)
        response: queue.Queue[MiRecord] = queue.Queue(maxsize=1)
        with self._write_lock:
            self._token += 1
            token = self._token
            with self._pending_lock:
                self._pending[token] = response
            try:
                process.stdin.write(f"{token}{command}\n")
                process.stdin.flush()
            except OSError as exc:
                with self._pending_lock:
                    self._pending.pop(token, None)
                raise GdbMiError(f"Unable to write to GDB: {exc}") from exc
        try:
            record = response.get(timeout=timeout)
        except queue.Empty as exc:
            with self._pending_lock:
                self._pending.pop(token, None)
            raise GdbMiError(f"GDB command timed out: {command.split()[0]}") from exc
        if record.cls == "error":
            raise GdbMiError(str(record.payload.get("msg", "GDB command failed")))
        return record.payload

    def close(self, force: bool = False) -> None:
        process = self._process
        if not process:
            return
        if process.poll() is None and not force:
            try:
                self.command("-gdb-exit", timeout=2.0)
            except GdbMiError:
                process.terminate()
        elif process.poll() is None:
            # Used when remote attach itself timed out. Sending another MI
            # command or Ctrl-C at that point confuses pyOCD's all-stop RSP
            # session, so terminate the wedged client directly.
            process.terminate()
        if process.poll() is None:
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        self._process = None

    def _read_stdout(self) -> None:
        assert self._process and self._process.stdout
        for line in self._process.stdout:
            try:
                record = parse_mi_record(line)
            except GdbMiError as exc:
                self.stderr.append(str(exc))
                continue
            if not record:
                continue
            if record.prefix == "^" and record.token is not None:
                with self._pending_lock:
                    waiter = self._pending.pop(record.token, None)
                if waiter:
                    waiter.put(record)
                    continue
            if record.prefix in "~@&?":
                self.stream.append(str(record.payload))
                if len(self.stream) > 200:
                    del self.stream[:-200]
            if self._event_handler:
                self._event_handler(record)

    def _read_stderr(self) -> None:
        assert self._process and self._process.stderr
        for line in self._process.stderr:
            self.stderr.append(line.rstrip())
            if len(self.stderr) > 100:
                del self.stderr[:-100]
