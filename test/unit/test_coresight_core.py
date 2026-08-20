import pytest

from pyocd.core import exceptions
from pyocd.core.target import Target
from pyocd.coresight.ap import APv1Address, MEM_AP
from pyocd.coresight.dap import DebugPort
from pyocd.coresight.discovery import ADIv5Discovery
from pyocd.coresight.dwt import DWT, Watchpoint


def test_ap_address_ordering_includes_dp_index():
    assert APv1Address(0, dp=0) < APv1Address(1, dp=1)
    assert APv1Address(2, dp=0) < APv1Address(2, dp=1)
    assert sorted((APv1Address(2, dp=1), APv1Address(0, dp=1), APv1Address(2, dp=0))) == [
        APv1Address(0, dp=1), APv1Address(2, dp=0), APv1Address(2, dp=1)
    ]


def test_adiv5_discovery_scans_ap_selector_255():
    class Options:
        def get(self, name):
            return {'scan_all_aps': True, 'adi.v5.max_invalid_ap_count': 2}[name]

    class Session:
        options = Options()
        log_tracebacks = False

    class DP:
        valid_aps = None

        def read_ap(self, addr):
            return 0x24770001 if (addr >> 24) == 255 else 0

    class Target:
        dp = DP()
        session = Session()

    discovery = ADIv5Discovery(Target())
    discovery._find_aps()

    assert discovery.dp.valid_aps == [255]


def test_adiv6_debug_port_has_no_base_address_until_valid_baseptr():
    class Session:
        def subscribe(self, *args):
            pass

    class Target:
        session = Session()

    assert DebugPort(object(), Target()).base_address is None


def test_mem_ap_invalidates_csw_cache_on_transfer_error():
    class Probe:
        def lock(self):
            pass

        def unlock(self):
            pass

    class DP:
        probe = Probe()

        def write_ap(self, addr, data):
            raise exceptions.TransferFaultError('write failed')

    ap = object.__new__(MEM_AP)
    ap.dp = DP()
    ap._reg_offset = 0
    ap._cached_csw = -1
    ap.address = APv1Address(0)

    with pytest.raises(exceptions.TransferFaultError):
        ap.write_reg(0, 0x1234)

    assert ap._cached_csw == -1


def test_mem_ap_block_write_does_not_slice_remaining_data():
    class PageData:
        def __init__(self, values):
            self.values = values

        def __len__(self):
            return len(self.values)

        def __getitem__(self, key):
            if isinstance(key, slice):
                # Page slices are bounded. An open-ended remainder slice would re-copy the
                # whole remaining transfer on every page.
                assert key.stop is not None
            return self.values[key]

    ap = object.__new__(MEM_AP)
    ap._address_mask = 0xffffffff
    ap.auto_increment_page_size = 8
    ap.lock = lambda: None
    ap.unlock = lambda: None
    written_pages = []
    ap._write_block32_page = lambda addr, data: written_pages.append((addr, list(data)))

    MEM_AP._write_memory_block32(ap, 0, PageData([1, 2, 3, 4, 5]))

    assert written_pages == [(0, [1, 2]), (8, [3, 4]), (16, [5])]


def test_dwt_rejects_invalid_watchpoint_size_without_consuming_comparator():
    dwt = object.__new__(DWT)
    dwt.dwt_configured = True
    dwt.watchpoints = [Watchpoint(0, dwt)]
    dwt.watchpoint_used = 0

    assert not dwt.set_watchpoint(0x1000, 3, Target.WatchpointType.WRITE)
    assert dwt.watchpoints[0].func == 0
    assert dwt.watchpoint_used == 0
