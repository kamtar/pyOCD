import pytest

from pyocd.web.gdb_mi import GdbMiError, parse_mi_record, quote_mi


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
