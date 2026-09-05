"""Hardware-free regressions for debugger execution and breakpoint integrity."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pyocd.core import exceptions
from pyocd.core.memory_map import MemoryMap, RamRegion
from pyocd.core.target import Target
from pyocd.coresight.cortex_m import CortexM
from pyocd.coresight.dwt import DWT, DWTv2, Watchpoint
from pyocd.debug.breakpoints.manager import BreakpointManager
from pyocd.debug.breakpoints.software import SoftwareBreakpointProvider
from pyocd.gdbserver.context_facade import GDBDebugContextFacade
from pyocd.gdbserver.gdbserver import GDBServer


class MemoryAP:
    def __init__(self):
        self.memory = {}
        self.fail = False

    def write_memory(self, addr, value, transfer_size=32):
        if self.fail:
            self.fail = False
            raise exceptions.TransferError('test transfer failure')
        for i in range(transfer_size // 8):
            self.memory[addr + i] = (value >> (8 * i)) & 255

    def read_memory(self, addr, transfer_size=32, now=True):
        value = sum(self.memory.get(addr + i, 0) << (8 * i) for i in range(transfer_size // 8))
        return value if now else lambda: value

    def write16(self, addr, value):
        self.write_memory(addr, value, 16)

    def read16(self, addr):
        return self.read_memory(addr, 16)

    def read32(self, addr):
        return self.read_memory(addr)

    def write_memory_block8(self, addr, data):
        for i, value in enumerate(data):
            self.write_memory(addr + i, value, 8)

    def write_memory_block32(self, addr, data):
        for i, value in enumerate(data):
            self.write_memory(addr + 4 * i, value)

    def read_memory_block32(self, addr, size):
        return [self.read32(addr + 4 * i) for i in range(size)]


@pytest.fixture
def core():
    core = object.__new__(CortexM)
    core._ap = MemoryAP()
    core._session = SimpleNamespace(subscribe=Mock(), notify=Mock(), options={
        'cpu.step.instruction.timeout': 0.001, 'reset.halt_timeout': 0.001})
    core.memory_map = MemoryMap(RamRegion(start=0x20000000, length=0x1000))
    core._core_number = 0
    core._run_token = 0
    core.dwt = None
    core.flush = Mock()
    core.invalidate_instruction_cache = Mock()
    core.bp_manager = BreakpointManager(core)
    core.provider = SoftwareBreakpointProvider(core)
    core.bp_manager.add_provider(core.provider)
    core.is_halted = lambda: True
    core.read_core_register_raw = Mock(return_value=0x20000000)
    core.session.notify.side_effect = lambda event, *args: core.bp_manager.flush(True) if event == Target.Event.PRE_RUN else None
    return core


def install(core, addr=0x20000000, instr=0x1234):
    core.ap.write16(addr, instr)
    assert core.bp_manager.set_breakpoint(addr, Target.BreakpointType.SW)
    core.bp_manager.flush()
    return core.bp_manager.find_breakpoint(addr)


def test_word_read_hides_breakpoints_in_later_words(core):
    install(core, 0x20000004)
    install(core, 0x20000006, 0x5678)
    assert core.read_memory_block32(0x20000000, 2) == [0, 0x56781234]


@pytest.mark.parametrize('mode', ['byte', 'halfword', 'word', 'block8', 'block32'])
def test_writes_preserve_breakpoint_and_update_restored_code(core, mode):
    bp = install(core)
    if mode == 'byte':
        core.write_memory(bp.addr + 1, 0xab, 8)
        expected = 0xab34
    elif mode == 'halfword':
        core.write_memory(bp.addr, 0xabcd, 16)
        expected = 0xabcd
    elif mode == 'word':
        core.write_memory(bp.addr, 0x8765abcd)
        expected = 0xabcd
    elif mode == 'block8':
        core.write_memory_block8(bp.addr - 1, [0xff, 0xcd, 0xab, 0xee])
        expected = 0xabcd
    else:
        core.write_memory_block32(bp.addr, [0x8765abcd])
        expected = 0xabcd
    assert core.ap.read16(bp.addr) == 0xbe00
    assert core.read16(bp.addr) == expected
    core.bp_manager.remove_breakpoint(bp.addr)
    core.bp_manager.flush()
    assert core.ap.read16(bp.addr) == expected


def test_failed_write_does_not_commit_saved_instruction(core):
    bp = install(core)
    core.ap.fail = True
    with pytest.raises(exceptions.TransferError):
        core.write_memory(bp.addr, 0xabcd, 16)
    assert bp.original_instr == 0x1234
    assert core.ap.read16(bp.addr) == 0xbe00


def test_deferred_write_failure_does_not_commit_saved_instruction(core):
    bp = install(core)
    core.flush.side_effect = exceptions.TransferError('deferred failure')
    with pytest.raises(exceptions.TransferError):
        core.write_memory(bp.addr, 0xabcd, 16)
    assert bp.original_instr == 0x1234


def test_unrelated_write_does_not_force_flush(core):
    install(core)
    core.flush.reset_mock()
    core.write_memory(0x20000010, 42)
    core.flush.assert_not_called()


def test_step_executes_saved_instruction_and_reinstalls_breakpoint(core):
    bp = install(core)
    core.read32 = Mock(return_value=CortexM.C_DEBUGEN | CortexM.S_HALT)
    executed = []
    def execute(*args):
        executed.append(core.ap.read16(bp.addr))
        return False
    core._step_instruction = execute
    core.step()
    assert executed == [0x1234]
    assert core.ap.read16(bp.addr) == 0xbe00
    assert bp.enabled


def test_failed_step_restores_breakpoint_and_interrupt_mask(core):
    bp = install(core)
    core.read32 = Mock(return_value=CortexM.C_DEBUGEN | CortexM.S_HALT)
    core._step_instruction = Mock(side_effect=exceptions.TimeoutError('step timed out'))
    core.write32 = Mock()
    with pytest.raises(exceptions.TimeoutError):
        core.step()
    assert core.ap.read16(bp.addr) == 0xbe00
    assert core.write32.call_args.args == (CortexM.DHCSR, CortexM.DBGKEY | CortexM.C_DEBUGEN | CortexM.C_HALT)


@pytest.mark.parametrize('cancelled', [False, True])
def test_step_timeout_or_cancellation_requests_and_confirms_halt(core, cancelled):
    core.write32 = Mock()
    def status(addr):
        return CortexM.S_HALT if core.write32.call_args.args[1] & CortexM.C_HALT else 0
    core.read32 = Mock(side_effect=status)
    if cancelled:
        assert core._step_instruction(CortexM.C_DEBUGEN | CortexM.C_STEP, 0.001, lambda: True)
    else:
        with pytest.raises(exceptions.TimeoutError, match='instruction step timed out'):
            core._step_instruction(CortexM.C_DEBUGEN | CortexM.C_STEP, 0.001, None)
    assert core.write32.call_args.args[1] & CortexM.C_HALT
    assert not core.write32.call_args.args[1] & CortexM.C_STEP


def test_step_requires_observed_halt_not_just_halt_request(core):
    core.read32 = Mock(return_value=CortexM.C_DEBUGEN | CortexM.C_HALT)
    with pytest.raises(exceptions.DebugError, match='core not halted'):
        core.step()


@pytest.mark.parametrize('packet, expected', [(b'c', None), (b's', None), (b'c0', 0),
    (b's20000000#00', 0x20000000), (b'C05;100', 0x100), (b'S05;0', 0),
    (b'C05', None), (b'c;100', 0x100)])
def test_rsp_execution_addresses(packet, expected):
    assert GDBServer._get_resume_step_addr(None, packet) == expected


def dwt_with_comparators(count=4):
    dwt = DWTv2(MemoryAP(), addr=0)
    dwt.dwt_configured = True
    dwt.watchpoints = [Watchpoint(0x20 + i * 16, dwt) for i in range(count)]
    return dwt


def test_dwt_range_allocation_duplicate_and_removal():
    dwt = dwt_with_comparators()
    assert dwt.set_watchpoint(0x20000001, 7, Target.WatchpointType.WRITE)
    assert [(w.addr, w.size) for w in dwt.watchpoints if w.func] == [
        (0x20000001, 1), (0x20000002, 2), (0x20000004, 4)]
    assert dwt.set_watchpoint(0x20000001, 7, Target.WatchpointType.WRITE)
    assert dwt.watchpoint_used == 3
    assert len(dwt.get_watchpoints()) == 1
    dwt.remove_watchpoint(0x20000001, 7, Target.WatchpointType.WRITE)
    assert dwt.watchpoint_used == 0
    assert not dwt.get_watchpoints()
    for watch in dwt.watchpoints:
        assert dwt.ap.read32(watch.comp_register_addr + 8) == 0


def test_dwt_exhaustion_does_not_modify_hardware():
    dwt = dwt_with_comparators(1)
    assert not dwt.set_watchpoint(0x20000000, 8, Target.WatchpointType.READ)
    assert dwt.ap.memory == {}
    assert dwt.watchpoint_used == 0


def test_dwt_failed_install_rolls_back():
    dwt = dwt_with_comparators()
    original = dwt.ap.write_memory
    def fail_second_address(addr, value, transfer_size=32):
        if addr == dwt.watchpoints[1].comp_register_addr:
            raise exceptions.TransferError('second comparator failed')
        original(addr, value, transfer_size)
    dwt.ap.write_memory = fail_second_address
    with pytest.raises(exceptions.TransferError):
        dwt.set_watchpoint(0x20000000, 8, Target.WatchpointType.READ)
    assert dwt.watchpoint_used == 0
    assert not dwt.get_watchpoints()
    assert dwt.ap.read32(dwt.watchpoints[0].comp_register_addr + 8) == 0


def test_dwt_read_to_clear_match_is_cached_and_mapped_to_group():
    dwt = dwt_with_comparators()
    assert dwt.set_watchpoint(0x20000000, 8, Target.WatchpointType.READ)
    function = dwt.watchpoints[1].comp_register_addr + 8
    dwt.ap.write_memory(function, dwt.ap.read32(function) | DWT.DWT_FUNCTION_MATCHED)
    matched = dwt.get_matched_watchpoints()
    dwt.ap.write_memory(function, 0)
    assert dwt.get_matched_watchpoints() == matched
    assert matched[0].addr == 0x20000000
    dwt.clear_watchpoint_matches()
    assert not dwt.get_matched_watchpoints()


@pytest.mark.parametrize('kind, field', [(Target.WatchpointType.READ, b'rwatch'),
    (Target.WatchpointType.WRITE, b'watch'), (Target.WatchpointType.READ_WRITE, b'awatch')])
def test_gdb_stop_reports_watchpoint(kind, field):
    facade = object.__new__(GDBDebugContextFacade)
    facade._context = SimpleNamespace(core=SimpleNamespace(get_watchpoint_hit=lambda: (kind, 0x20000000)))
    facade.get_signal_value = lambda: 5
    facade._get_reg_index_value_pairs = lambda names: b''
    assert facade.get_t_response() == b'T05' + field + b':20000000;'
    assert facade.get_t_response(force_signal=2) == b'T02'


def test_suspend_setup_failure_reinstalls_breakpoint(core):
    bp = install(core)
    core.ap.fail = True
    with pytest.raises(exceptions.TransferError):
        with core.provider.suspend_breakpoint(bp):
            pytest.fail('must not execute after failed restore')
    assert bp.enabled
    assert core.ap.read16(bp.addr) == 0xbe00


def test_failed_breakpoint_recovery_blocks_execution(core):
    bp = install(core)
    with pytest.raises(exceptions.DebugError, match='core is not halted'):
        with core.provider.suspend_breakpoint(bp):
            core.is_halted = lambda: False
    assert not bp.enabled
    with pytest.raises(exceptions.DebugError, match='recovery failed'):
        core.bp_manager.flush()


def test_range_step_stops_before_next_breakpoint(core):
    first = install(core)
    install(core, first.addr + 2, 0x5678)
    core.read32 = Mock(return_value=CortexM.C_DEBUGEN | CortexM.S_HALT)
    core.read_core_register_raw.side_effect = [first.addr, first.addr + 2]
    core._step_instruction = Mock(return_value=False)
    core.step(start=first.addr, end=first.addr + 16)
    core._step_instruction.assert_called_once()
    assert core.ap.read16(first.addr) == 0xbe00


def test_transfer_failure_during_step_attempts_recovery(core):
    core.write32 = Mock()
    core.read32 = Mock(side_effect=[exceptions.TransferError('lost transfer'), CortexM.S_HALT])
    with pytest.raises(exceptions.TransferError, match='lost transfer'):
        core._step_instruction(CortexM.C_DEBUGEN | CortexM.C_STEP, 0.001, None)
    assert core.write32.call_args.args[1] & CortexM.C_HALT


def test_failed_halt_recovery_is_bounded(core):
    core.write32 = Mock()
    core.read32 = Mock(return_value=0)
    with pytest.raises(exceptions.TimeoutError, match='could not halt core'):
        core._step_instruction(CortexM.C_DEBUGEN | CortexM.C_STEP, 0.001, lambda: True)


def test_dwt_unsupported_mode_rolls_back():
    dwt = dwt_with_comparators()
    dwt.ap.read32 = lambda address: 0
    assert not dwt.set_watchpoint(0x20000000, 8, Target.WatchpointType.READ)
    assert not dwt.get_watchpoints()
    assert dwt.watchpoint_used == 0
    assert dwt.ap.read_memory(dwt.watchpoints[0].comp_register_addr + 8) == 0


def test_dwt_remove_all_releases_entire_groups():
    dwt = dwt_with_comparators()
    assert dwt.set_watchpoint(0x20000000, 8, Target.WatchpointType.READ)
    assert dwt.set_watchpoint(0x20000010, 4, Target.WatchpointType.WRITE)
    dwt.remove_all_watchpoints()
    assert not dwt.get_watchpoints()
    assert dwt.watchpoint_used == 0


@pytest.mark.parametrize('addr, size', [(-1, 4), (0, 0), (0, -1), (0xffffffff, 2)])
def test_invalid_watchpoint_range_does_not_touch_hardware(addr, size):
    dwt = dwt_with_comparators()
    assert not dwt.set_watchpoint(addr, size, Target.WatchpointType.READ)
    assert not dwt.ap.memory


def test_gdb_step_applies_address_zero_before_execution():
    events = []
    server = SimpleNamespace(
        _get_resume_step_addr=lambda data: GDBServer._get_resume_step_addr(None, data),
        target_context=SimpleNamespace(write_core_register_raw=lambda reg, value: events.append((reg, value))),
        target=SimpleNamespace(step=lambda *args, **kwargs: events.append('step')),
        trace_capture=lambda: None, trace_flush=lambda: None, step_into_interrupt=False,
        get_t_response=lambda client: b'T05', create_rsp_packet=lambda data: data)
    client = SimpleNamespace(is_interrupted=lambda: False)
    assert GDBServer.step(server, client, b's0') == b'T05'
    assert events == [('pc', 0), 'step']


def test_watchpoint_hit_survives_simultaneous_step_halt(core):
    watch = SimpleNamespace(addr=0x20000000, func=6)
    core.dwt = SimpleNamespace(get_matched_watchpoints=lambda: [watch], WATCH_TYPE_TO_FUNCT=DWT.WATCH_TYPE_TO_FUNCT)
    core.read32 = lambda addr: CortexM.DFSR_HALTED | CortexM.DFSR_DWTTRAP
    assert core.get_watchpoint_hit() == (Target.WatchpointType.WRITE, 0x20000000)
