from pyocd.trace.events import TraceDataTraceEvent
from pyocd.trace.swo import SWOParser


def test_swo_data_trace_merge_preserves_zero_and_false_values():
    parser = SWOParser(object())
    parser._merge_data_trace_events(TraceDataTraceEvent(cmpn=1, pc=0))
    parser._merge_data_trace_events(
        TraceDataTraceEvent(cmpn=1, value=0, rnw=False, sz=1))

    merged = parser._pending_events[0]
    assert merged.pc == 0
    assert merged.value == 0
    assert merged.is_read is False
    assert merged.transfer_size == 1
