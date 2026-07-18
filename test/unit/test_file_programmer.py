from types import SimpleNamespace

from elftools.elf.constants import SH_FLAGS

from pyocd.flash.file_programmer import FileProgrammer


class FakeSegment(dict):
    def __init__(self, data, **values):
        super().__init__(values)
        self._data = data
        self.header = SimpleNamespace(
            p_type=values["p_type"], p_filesz=values["p_filesz"])

    def data(self):
        return self._data


class FakeElf:
    def __init__(self, sections):
        self._sections = sections

    def iter_sections(self):
        return iter(self._sections)


def section(offset, size, *, alloc=True, section_type="SHT_PROGBITS"):
    return {
        "sh_offset": offset,
        "sh_size": size,
        "sh_flags": SH_FLAGS.SHF_ALLOC if alloc else 0,
        "sh_type": section_type,
    }


def segment(data, *, offset, physical_address):
    return FakeSegment(
        data,
        p_type="PT_LOAD",
        p_offset=offset,
        p_paddr=physical_address,
        p_vaddr=physical_address,
        p_filesz=len(data),
    )


def test_elf_segment_excludes_page_aligned_headers_before_first_alloc_section():
    data = bytearray((value % 251 for value in range(0x2200)))
    image = section(0x1000, 0x1200)

    address, result = FileProgrammer._get_elf_segment_data(
        FakeElf([image]), segment(data, offset=0, physical_address=0))

    assert address == 0x1000
    assert result == data[0x1000:0x2200]


def test_elf_segment_uses_file_offset_to_derive_section_lma():
    data = bytearray((value % 251 for value in range(0x400)))
    ram_data = section(0x8100, 0x180)

    address, result = FileProgrammer._get_elf_segment_data(
        FakeElf([ram_data]), segment(data, offset=0x8000, physical_address=0x76000))

    assert address == 0x76100
    assert result == data[0x100:0x280]


def test_elf_segment_preserves_full_data_without_usable_section_table():
    data = bytearray(range(64))
    sections = [
        section(0, 16, alloc=False),
        section(16, 32, section_type="SHT_NOBITS"),
    ]

    address, result = FileProgrammer._get_elf_segment_data(
        FakeElf(sections), segment(data, offset=0, physical_address=0x2000))

    assert address == 0x2000
    assert result == data
