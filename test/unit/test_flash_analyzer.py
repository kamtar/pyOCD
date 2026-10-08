# pyOCD debugger
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace
from unittest import mock

import pytest

from pyocd.core.exceptions import FlashFailure
from pyocd.flash.flash import Flash, _ANALYZER_CODE


def test_compute_crcs_batches_commands_to_page_buffer():
    flash = object.__new__(Flash)
    flash.flash_algo = {
        'analyzer_address': 0x20000000,
        'page_size': 8,
    }
    flash.use_analyzer = True
    flash.page_buffers = [0x20001000]
    flash._region = SimpleNamespace(page_size=8)
    flash._call_function_and_wait = mock.Mock()

    target = mock.Mock()
    target.session.options.get.return_value = 30.0
    target.read_memory_block32.side_effect = [[10, 11], [12, 13], [14]]
    flash.target = target

    sectors = [(address, 4) for address in range(0, 20, 4)]
    assert flash.compute_crcs(sectors) == [10, 11, 12, 13, 14]

    assert target.write_memory_block32.call_args_list == [
        mock.call(0x20000000, _ANALYZER_CODE),
        mock.call(0x20001000, [2, 0x00010002]),
        mock.call(0x20001000, [0x00020002, 0x00030002]),
        mock.call(0x20001000, [0x00040002]),
    ]
    assert flash._call_function_and_wait.call_count == 3


def test_compute_crcs_rejects_unrepresentable_page_index_before_loading_analyzer():
    flash = object.__new__(Flash)
    flash.flash_algo = {
        'analyzer_address': 0x20000000,
        'page_size': 8,
    }
    flash.use_analyzer = True
    flash.page_buffers = [0x20001000]
    flash._region = SimpleNamespace(page_size=8)
    flash.target = mock.Mock()

    with pytest.raises(FlashFailure, match="analyzer address is out of range"):
        flash.compute_crcs([(0x10000, 1)])

    flash.target.write_memory_block32.assert_not_called()


def _flash_waiting_on_halted_target(canary):
    from pyocd.core.target import Target

    flash = object.__new__(Flash)
    flash.flash_algo_debug = False
    flash.begin_stack = 0x20002000
    flash.end_stack = 0x20001000
    target = mock.Mock()
    target.get_state.return_value = Target.State.HALTED
    target.read32.return_value = lambda: canary
    target.read_core_register.return_value = 0
    flash.target = target
    return flash


def test_wait_for_completion_defers_stack_canary_read():
    flash = _flash_waiting_on_halted_target(Flash._STACK_CANARY)

    assert flash.wait_for_completion(timeout=1.0) == 0

    # The canary read is queued so it is sent along with the R0 read instead of on its own.
    flash.target.read32.assert_called_once_with(0x20001000, now=False)
    flash.target.read_core_register.assert_called_once_with('r0')


def test_wait_for_completion_detects_stack_overflow():
    flash = _flash_waiting_on_halted_target(0)

    with pytest.raises(FlashFailure, match="overflowed stack"):
        flash.wait_for_completion(timeout=1.0)
