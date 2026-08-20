from types import SimpleNamespace
from unittest import mock

from pyocd.core.memory_map import FlashRegion
from pyocd.flash.builder import FlashBuilder
from pyocd.flash.flash import Flash, PageInfo, SectorInfo


class FakeFlash:
    def __init__(self):
        self.region = FlashRegion(start=0, length=0x100, sector_size=0x40, page_size=0x10)

    def get_sector_info(self, address):
        return SectorInfo(base_addr=address & ~0x3f, erase_weight=1, size=0x40)

    def get_page_info(self, address):
        return PageInfo(base_addr=address & ~0xf, program_weight=1, size=0x10)


class CrcFallbackFlash(FakeFlash):
    def __init__(self):
        self.region = FlashRegion(start=0x100000, length=0x100, sector_size=0x40, page_size=0x10)
        self.Operation = Flash.Operation
        self.target = mock.Mock()
        self.target.read_memory_block8.return_value = [0xff]
        self.init = mock.Mock()
        self.compute_crcs = mock.Mock()

    def get_flash_info(self):
        return SimpleNamespace(crc_supported=True)


def test_rebuilding_after_adding_data_discards_stale_pages_and_sectors():
    flash = FakeFlash()
    builder = FlashBuilder(flash)
    builder.add_data(0, [1])
    builder._build_sectors_and_pages(keep_unwritten=False)

    builder.add_data(0x20, [2])
    builder._build_sectors_and_pages(keep_unwritten=False)

    assert [page.addr for page in builder.page_list] == [0, 0x20]
    assert [sector.addr for sector in builder.sector_list] == [0]


def test_crc_analysis_falls_back_for_unrepresentable_page_index():
    flash = CrcFallbackFlash()
    builder = FlashBuilder(flash)
    builder.add_data(0x100000, [1])
    builder._build_sectors_and_pages(keep_unwritten=False)

    builder._compute_sector_erase_pages_and_weight(fast_verify=False)

    flash.compute_crcs.assert_not_called()
    assert builder.perf.analyze_type == FlashBuilder.FLASH_ANALYSIS_PARTIAL_PAGE_READ
