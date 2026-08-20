import io
from types import SimpleNamespace

from pyocd.debug import semihost
from pyocd.debug.elf import decoder
from pyocd.debug.rtt import GenericRTTDownChannel


def test_internal_semihost_readc_returns_eof_instead_of_raising():
    handler = semihost.InternalSemihostIOHandler()
    handler.open_files[semihost.STDIN_FD] = io.BytesIO()

    assert handler.readc() == -1


def test_semihost_string_read_stops_on_empty_successful_read():
    context = SimpleNamespace(read_memory_block8=lambda address, length: [])
    agent = semihost.SemihostAgent(context)

    assert agent.get_data(0x1000) == b""


def test_elf_symbol_decoder_accepts_stripped_elf(monkeypatch):
    class FakeELFFile:
        def get_section_by_name(self, name):
            return None

    monkeypatch.setattr(decoder, "ELFFile", FakeELFFile)
    symbols = decoder.ElfSymbolDecoder(FakeELFFile())

    assert symbols.get_symbol_for_address(0x1000) is None
    assert symbols.get_symbol_for_name("main") is None


def test_rtt_blocking_empty_write_reports_zero_bytes():
    channel = object.__new__(GenericRTTDownChannel)
    channel.size = 1
    channel._buffer_address = 1

    assert channel.write(b"", blocking=True) == 0
