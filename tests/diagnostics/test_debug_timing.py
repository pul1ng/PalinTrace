import pytest

from palinreplay.diagnostics.timing import debug_timing, is_tracing, span


def test_hierarchy_self_time_exception_redaction_and_context_cleanup(monkeypatch):
    import palinreplay.diagnostics.timing as timing

    clock = [10.0]
    monkeypatch.setattr(timing, "monotonic", lambda: clock[0])
    with debug_timing(True) as timer:
        timer.started = 10.0
        with span("root"):
            clock[0] = 12.0
            with pytest.raises(KeyboardInterrupt), span("child", step=3):
                clock[0] = 15.0
                raise KeyboardInterrupt("PRIVATE-DO-NOT-LOG")
            clock[0] = 18.0
        assert is_tracing()
    assert not is_tracing()
    report = timer.snapshot()
    root, child = report["spans"]
    assert root["parent_id"] is None and child["parent_id"] == root["id"]
    assert root["duration_sec"] == 8 and root["self_sec"] == 5
    assert child["duration_sec"] == child["self_sec"] == 3
    assert child["offset_sec"] == 2 and child["step"] == 3
    assert child["status"] == "raised" and child["exception_type"] == "KeyboardInterrupt"
    assert "PRIVATE" not in str(report) and "_children_sec" not in str(report)
    assert report["totals"]["child"]["raised"] == 1
    assert report["timeline_complete"]


def test_disabled_trace_does_not_read_clock_and_nested_sessions_restore(monkeypatch):
    import palinreplay.diagnostics.timing as timing

    def unexpected():
        pytest.fail("disabled trace must not read the clock")

    with debug_timing(True) as parent:
        with debug_timing(False) as disabled:
            assert disabled is None and not is_tracing()
            with monkeypatch.context() as patch:
                patch.setattr(timing, "monotonic", unexpected)
                with span("off"):
                    pass
        with span("parent"):
            pass
    assert [row["name"] for row in parent.snapshot()["spans"]] == ["parent"]


def test_bounded_timeline_keeps_complete_aggregates():
    with debug_timing(True, max_spans=2) as timing:
        with span("parent"):
            for _ in range(4):
                with span("child"):
                    pass
    report = timing.snapshot()
    assert not report["timeline_complete"] and report["dropped_spans"] == 3
    assert len(report["spans"]) == 2
    assert report["totals"]["child"]["count"] == 4
    assert report["totals"]["parent"]["self_sec"] >= 0
