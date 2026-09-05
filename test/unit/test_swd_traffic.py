from types import SimpleNamespace

import pytest

from pyocd.utility.swd_traffic import SwdTrafficRecorder


class FakeProbe:
    def read_dp(self, addr, now=True):
        def result():
            return 0x12000000 | addr

        return result if not now else result()

    def write_ap(self, addr, data):
        self.last_write = (addr, data)

    def read_ap_multiple(self, addr, count=1, now=True):
        def result():
            return [addr + index for index in range(count)]

        return result if not now else result()

    def swd_sequence(self, sequences):
        return 0, [bytes((0,)) for sequence in sequences if len(sequence) == 1]


def test_probe_binding_captures_grouped_register_data_and_deferred_reads():
    recorder = SwdTrafficRecorder(max_records=20)
    probe = FakeProbe()
    recorder.bind_probe(probe)
    recorder.set_enabled(True)

    with recorder.operation("Breakpoint set", "breakpoint", {"address": "0x08000100"}):
        probe.write_ap(0x10, 0x12345678)
        callback = probe.read_dp(0, now=False)
        assert callback() == 0x12000000

    batch = recorder.snapshot()
    assert [item["operation"] for item in batch["transactions"]] == ["AP write", "DP read"]
    assert batch["transactions"][0]["data"] == "0x12345678"
    assert batch["transactions"][1]["data"] == "0x12000000"
    assert batch["transactions"][0]["group_id"] == batch["transactions"][1]["group_id"]
    assert batch["groups"][0]["name"] == "Breakpoint set"
    assert batch["groups"][0]["transaction_count"] == 2


def test_nested_swd_sequence_is_not_double_counted():
    class SequenceProbe(FakeProbe):
        def read_dp(self, addr, now=True):
            self.swd_sequence(((8, 0xA5), (4,)))
            return super().read_dp(addr, now)

    recorder = SwdTrafficRecorder(max_records=20)
    probe = SequenceProbe()
    recorder.bind_probe(probe)
    recorder.set_enabled(True)

    assert probe.read_dp(4) == 0x12000004
    operations = [item["operation"] for item in recorder.snapshot()["transactions"]]
    assert operations == ["DP read"]


def test_failed_transfer_is_retained_and_buffer_is_bounded():
    class FailingProbe(FakeProbe):
        def read_dp(self, addr, now=True):
            raise RuntimeError("no ACK")

    recorder = SwdTrafficRecorder(max_records=2)
    probe = FailingProbe()
    recorder.bind_probe(probe)
    recorder.set_enabled(True)

    with pytest.raises(RuntimeError, match="no ACK"):
        probe.read_dp(0)
    probe.write_ap(0, 1)
    probe.write_ap(4, 2)
    batch = recorder.snapshot()
    assert len(batch["transactions"]) == 2
    assert batch["transactions"][-1]["data"] == "0x00000002"
    assert batch["dropped"] == 1


def test_target_notifications_create_logical_groups():
    recorder = SwdTrafficRecorder()
    probe = FakeProbe()
    recorder.bind_probe(probe)
    recorder.set_enabled(True)
    recorder.handle_notification(SimpleNamespace(
        event=SimpleNamespace(name="PRE_RUN"),
        data=SimpleNamespace(name="RESUME"), source=object()))
    group_id = recorder.current_group_id()
    probe.write_ap(0, 1)
    recorder.handle_notification(SimpleNamespace(
        event=SimpleNamespace(name="POST_RUN"), data=None, source=object()))
    assert recorder.current_group_id() is None
    assert recorder.snapshot()["groups"][0]["id"] == group_id
    assert recorder.snapshot()["groups"][0]["name"] == "Resume"
