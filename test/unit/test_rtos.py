from pyocd.rtos.argon import TargetList as ArgonTargetList
from pyocd.rtos.provider import ThreadProvider
from pyocd.rtos.rtx5 import TargetList as RTX5TargetList
from pyocd.rtos.threadx import TX_THREAD_ID, THREAD_NEXT_OFFSET, TargetList as ThreadXTargetList
from pyocd.rtos.zephyr import TargetList as ZephyrTargetList


class MemoryContext:
    def __init__(self, memory):
        self.memory = memory

    def read32(self, address):
        return self.memory[address]


class Target:
    def __init__(self):
        self.run_token = 0
        self.context = object()

    def get_target_context(self):
        return self.context


class Provider(ThreadProvider):
    def __init__(self, target):
        super().__init__(target)
        self.invalidated = 0
        self.built = 0

    def _build_thread_list(self):
        self.built += 1

    def get_threads(self):
        return []

    def get_thread(self, threadId):
        return None

    def invalidate(self):
        self.invalidated += 1


def test_enabling_target_reads_builds_without_another_run():
    target = Target()
    provider = Provider(target)

    provider.update_threads()
    provider.read_from_target = True
    provider.update_threads()

    assert provider.invalidated == 1
    assert provider.built == 1


def test_threadx_list_cycle_terminates():
    memory = {
        0x100: 0x200,
        0x200: TX_THREAD_ID,
        0x200 + THREAD_NEXT_OFFSET: 0x300,
        0x300: TX_THREAD_ID,
        0x300 + THREAD_NEXT_OFFSET: 0x300,
    }

    assert list(ThreadXTargetList(MemoryContext(memory), 0x100)) == [0x200, 0x300]


def test_argon_list_cycle_terminates():
    memory = {
        0x100: 0x200,
        0x200: 0x300,
        0x208: 0x500,
        0x300: 0x300,
        0x308: 0x600,
    }

    assert list(ArgonTargetList(MemoryContext(memory), 0x100)) == [0x500, 0x600]


def test_zephyr_list_cycle_terminates():
    memory = {0x100: 0x200, 0x204: 0x300, 0x304: 0x300}

    assert list(ZephyrTargetList(MemoryContext(memory), 0x100, 4)) == [0x200, 0x300]


def test_rtx5_list_cycle_terminates():
    memory = {0x100: 0x200, 0x204: 0x300, 0x304: 0x300}

    assert list(RTX5TargetList(MemoryContext(memory), 0x100, 4)) == [0x200, 0x300]
