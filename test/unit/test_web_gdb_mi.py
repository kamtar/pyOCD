import queue
import threading

import pytest

from pyocd.web.gdb_mi import GdbMiClient, GdbMiError, parse_mi_record, quote_mi


def test_parse_nested_stack_record():
    record = parse_mi_record(
        '17^done,stack=[frame={level="0",addr="0x08000100",func="main",'
        'file="main.c",line="12"},frame={level="1",func="reset"}]')

    assert record.token == 17
    assert record.cls == "done"
    assert record.payload["stack"][0]["frame"]["func"] == "main"
    assert record.payload["stack"][1]["frame"]["level"] == "1"


def test_parse_variables_and_escaped_strings():
    record = parse_mi_record(
        '3^done,variables=[{name="message",value="\\\"hello\\\"",type="char *"},'
        '{name="counter",value="2"}]')

    assert record.payload["variables"][0]["name"] == "message"
    assert record.payload["variables"][0]["value"] == '"hello"'
    assert record.payload["variables"][1]["name"] == "counter"


def test_parse_c_string_octal_and_non_json_escapes():
    record = parse_mi_record(r'4^done,value="\377\a\v"')

    assert record.payload["value"] == "\xff\a\v"


@pytest.mark.parametrize("prefix", ["~", "@", "&"])
def test_parse_stream_records_with_c_string_escapes(prefix):
    record = parse_mi_record(rf'{prefix}"hello\040world\a\v\"\\\n"')

    assert record.prefix == prefix
    assert record.cls == "stream"
    assert record.payload == 'hello world\a\v"\\\n'


class _EofProcess:
    def __init__(self):
        self.stdout = iter(())
        self.stderr = None
        self.stdin = None

    def poll(self):
        return None


class _RecordingInput:
    def __init__(self):
        self.written = []
        self.write_event = threading.Event()

    def write(self, data):
        self.written.append(data)
        self.write_event.set()

    def flush(self):
        pass


def test_gdb_reader_eof_wakes_pending_commands():
    client = object.__new__(GdbMiClient)
    client._pending = {}
    client._pending_lock = threading.Lock()
    client._write_lock = threading.Lock()
    client._process = _EofProcess()
    client._reader_error = None
    client.stderr = []
    client.stream = []
    client._event_handler = None

    response = queue.Queue(maxsize=1)
    client._pending[23] = response

    client._read_stdout(client._process)

    assert client._pending == {}
    record = response.get_nowait()
    assert record.cls == "error"
    assert "disconnected" in record.payload["msg"]


def test_gdb_command_fails_immediately_when_reader_reaches_eof():
    process = _EofProcess()
    process.stdin = _RecordingInput()
    client = object.__new__(GdbMiClient)
    client._pending = {}
    client._pending_lock = threading.Lock()
    client._write_lock = threading.Lock()
    client._token = 0
    client._process = process
    client._reader_error = None
    client.stderr = []
    client.stream = []
    client._event_handler = None
    error = []

    def wait_for_command():
        try:
            client.command("-list-thread-groups", timeout=2.0)
        except GdbMiError as exc:
            error.append(exc)

    command_thread = threading.Thread(target=wait_for_command)
    command_thread.start()
    assert process.stdin.write_event.wait(1.0)
    client._read_stdout(process)
    command_thread.join(1.0)

    assert not command_thread.is_alive()
    assert len(error) == 1
    assert "disconnected" in str(error[0])
    assert client._pending == {}


def test_parse_async_stop_record():
    record = parse_mi_record(
        '*stopped,reason="end-stepping-range",frame={addr="0x100",func="work"}')
    assert record.prefix == "*"
    assert record.cls == "stopped"
    assert record.payload["frame"]["func"] == "work"


def test_prompt_is_ignored_and_malformed_value_fails():
    assert parse_mi_record("(gdb)") is None
    with pytest.raises(GdbMiError):
        parse_mi_record('1^done,value={name="x"')


def test_quote_mi_uses_c_compatible_escaping():
    assert quote_mi('C:\\firmware files\\app.elf') == '"C:\\\\firmware files\\\\app.elf"'
