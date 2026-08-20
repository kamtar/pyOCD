from types import SimpleNamespace
from unittest import mock

from pyocd.core.memory_map import MemoryMap
from pyocd.coresight.coresight_target import CoreSightTarget
from pyocd.target.family import target_lpc5500, target_nRF91


def test_lpc5500_only_creates_core1_when_ap1_is_enabled(monkeypatch):
    created_core1 = []

    class FakeCore0:
        def __init__(self, *args):
            pass

        def init(self):
            pass

    class FakeCore1:
        def __init__(self, *args):
            created_core1.append(args[1])

        def init(self):
            pass

    monkeypatch.setattr(target_lpc5500, 'CortexM_LPC5500', FakeCore0)
    monkeypatch.setattr(target_lpc5500, 'CortexM_v8M', FakeCore1)

    target = object.__new__(target_lpc5500.LPC5500Family)
    target.dp = SimpleNamespace(aps={
        0: SimpleNamespace(is_enabled=True),
        1: SimpleNamespace(is_enabled=False),
    })
    target._session = SimpleNamespace(log_tracebacks=False)
    target.memory_map = MemoryMap()
    target.add_core = mock.Mock()

    target.create_lpc55xx_cores()

    assert created_core1 == []


def test_nrf91_adds_uicr_to_default_memory_map(monkeypatch):
    def fake_core_sight_init(self, session, memory_map=None):
        self.memory_map = memory_map.clone() if memory_map is not None else MemoryMap()

    monkeypatch.setattr(CoreSightTarget, '__init__', fake_core_sight_init)

    target = target_nRF91.NRF91(SimpleNamespace())

    assert target.memory_map.get_region_for_address(0x00ff8000) is not None
