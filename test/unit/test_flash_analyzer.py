# pyOCD debugger
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace
from unittest import mock

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
