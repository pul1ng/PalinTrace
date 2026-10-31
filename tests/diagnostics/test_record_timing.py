import pytest

from palinreplay.diagnostics.record_timing import (
    flow_scope,
    record_phase,
    record_step,
    record_timing,
)


def test_document_steps_partition_wall_time_without_double_counting(monkeypatch):
    import palinreplay.diagnostics.record_timing as module

    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    with record_timing(True) as timing:
        timing.started = 0.0
        with flow_scope("1.1"):
            clock[0] = 2.0
        with record_step(0), flow_scope("1.6"):
            with flow_scope("S1"):
                clock[0] = 3.0
            with flow_scope("S2"):
                clock[0] = 4.0
                with flow_scope("S4"):
                    with flow_scope("S3"):
                        clock[0] = 6.0
                    clock[0] = 7.0
                clock[0] = 8.0
            with flow_scope("S5"):
                clock[0] = 9.0
            with flow_scope("S6"):
                clock[0] = 10.0
        record_phase("steps")
        with record_step(1), flow_scope("2.1"):
            clock[0] = 11.0
            with flow_scope("2.2"):
                with flow_scope("2.1"):
                    clock[0] = 11.5
                clock[0] = 13.0
            clock[0] = 14.0
        clock[0] = 15.0
        record_phase("finalization")
        for code, end in [("3.1", 16.0), ("3.2", 18.0), ("3.3", 19.0), ("3.4", 20.0)]:
            with flow_scope(code):
                clock[0] = end
        record_phase("finalization")  # Repeated runner cleanup must not reset the stage.
    data = timing.snapshot()
    assert data["duration_sec"] == 20
    assert [p["duration_sec"] for p in data["phases"]] == [10, 5, 5]
    assert [p["other_sec"] for p in data["phases"]] == [0, 1, 0]
    capture = data["steps"][0]["capture"]
    assert capture["duration_sec"] == 8 and capture["other_sec"] == 0
    assert {r["id"]: r["duration_sec"] for r in capture["substeps"]} == {
        "S1": 1, "S2": 2, "S3": 2, "S4": 1, "S5": 1, "S6": 1,
    }
    assert timing.totals["2.1"]["duration_sec"] == 2.5
    assert timing.totals["2.2"]["duration_sec"] == 1.5


def test_disabled_timing_does_not_read_clock_or_leak_context(monkeypatch):
    import palinreplay.diagnostics.record_timing as module

    with record_timing(True) as outer:
        with record_timing(False) as disabled:
            assert disabled is None
            with monkeypatch.context() as patch:
                patch.setattr(module, "monotonic", lambda: pytest.fail("disabled clock"))
                with record_step(9), flow_scope("2.2"):
                    record_phase("steps")
        with flow_scope("1.1"):
            pass
    assert set(outer.totals) == {"1.1"}
    assert module.current() is None


def test_errors_missing_measurements_and_detail_limit_keep_truthful_totals():
    with record_timing(True, max_steps=1) as timing:
        with record_step(0), flow_scope("1.6"):
            pass
        record_phase("steps")
        for index in (1, 2):
            with pytest.raises(KeyboardInterrupt), record_step(index), flow_scope("2.2"):
                raise KeyboardInterrupt("PRIVATE")
        record_phase("finalization")
    data = timing.snapshot()
    assert len(data["steps"]) == 1 and not data["step_details_complete"]
    model = next(r for r in data["phases"][1]["substeps"] if r["id"] == "2.2")
    assert model["count"] == model["raised"] == 2
    assert "PRIVATE" not in str(data)
    missing = data["steps"][0]["capture"]["substeps"][0]
    assert missing["duration_sec"] is None and missing["measurement"] == "not_observed"


def test_copied_worker_context_does_not_double_count_parent_wall_time():
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    with record_timing(True) as timing, ThreadPoolExecutor(max_workers=1) as executor:
        def child():
            with flow_scope("S2"):
                record_phase("finalization")

        with flow_scope("1.6"):
            executor.submit(copy_context().run, child).result()
    assert set(timing.totals) == {"1.6"}
    assert timing.phase == 0


def test_live_events_arrive_at_boundaries_and_match_exclusive_timing(monkeypatch):
    import palinreplay.diagnostics.record_timing as module

    clock, events = [0.0], []
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    with record_timing(True, progress=events.append) as timing:
        timing.started = 0.0
        assert events[-1].phase == "flow_phase" and events[-1].code == "1"
        record_phase("steps")
        with record_step(1), flow_scope("2.1"):
            assert (events[-1].phase, events[-1].code, events[-1].step) == ("flow_start", "2.1", 1)
            clock[0] = 1
            with flow_scope("2.2"):
                clock[0] = 4
            assert events[-1].phase == "flow_end" and events[-1].duration_sec == 3
            clock[0] = 5
        assert events[-1].duration_sec == 2 and events[-1].inclusive_sec == 5
        with record_step(2), flow_scope("2.1"):
            clock[0] = 7
        assert events[-1].step == 2
        record_phase("finalization")
        clock[0] = 8
    phases = [e for e in events if e.phase == "flow_phase" and e.status == "finished"]
    assert [e.duration_sec for e in phases] == [p["duration_sec"] for p in timing.snapshot()["phases"]]
    assert [e.code for e in phases] == ["1", "2", "3"]
    for code, row in timing.totals.items():
        completed = [e for e in events if e.phase == "flow_end" and e.code == code]
        assert sum(e.duration_sec for e in completed) == row["duration_sec"]
        assert sum(e.inclusive_sec for e in completed) == row["inclusive_sec"]


def test_disabled_timing_emits_no_live_events():
    events = []
    with record_timing(False, progress=events.append):
        with record_step(1), flow_scope("2.1"):
            record_phase("steps")
    assert events == []


def test_broken_live_observer_is_disabled_without_affecting_measurements():
    events = []

    def broken(event):
        events.append(event)
        raise OSError("PRIVATE terminal failure")

    with record_timing(True, progress=broken) as timing:
        with flow_scope("1.1"):
            pass
        record_phase("steps")
    assert len(events) == 1 and timing.totals["1.1"]["count"] == 1


@pytest.mark.parametrize("interrupt_at", ["flow_start", "flow_end", "flow_phase"])
def test_live_observer_interrupt_cleans_scope_and_context(interrupt_at):
    import palinreplay.diagnostics.record_timing as module

    fired = False

    def interrupt(event):
        nonlocal fired
        if not fired and event.phase == interrupt_at and event.status != "started":
            fired = True
            raise KeyboardInterrupt("PRIVATE cancellation")

    with pytest.raises(KeyboardInterrupt):
        with record_timing(True, progress=interrupt) as timing:
            with flow_scope("1.1"):
                pass
    assert fired and not timing.stack and module.current() is None


def test_parts_partition_main_category_even_when_same_category_is_nested(monkeypatch):
    import palinreplay.diagnostics.record_timing as module

    clock, events = [0.0], []
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    with record_timing(True, progress=events.append) as timing:
        timing.started = 0.0
        for part, end in [("1.2.1", 1), ("1.2.2", 3), ("1.2.3", 4)]:
            with flow_scope("1.2", part=part):
                assert events[-1].subcode == part
                clock[0] = end
        record_phase("steps")
        with record_step(1), flow_scope("2.1", part="2.1.1"):
            clock[0] = 5
            with flow_scope("2.2"):
                with flow_scope("2.1", part="2.1.2"):
                    clock[0] = 6
                clock[0] = 9
            clock[0] = 10
        record_phase("finalization")
    flow = timing.snapshot()
    config = next(r for r in flow["phases"][0]["substeps"] if r["id"] == "1.2")
    assert config["duration_sec"] == 4
    assert [r["duration_sec"] for r in config["parts"]] == [1, 2, 1]
    query = flow["steps"][0]["substeps"][0]
    assert query["duration_sec"] == 3 and query["unclassified_sec"] == 0
    assert [(r["id"], r["duration_sec"]) for r in query["parts"]] == [("2.1.1", 2), ("2.1.2", 1)]
    starts = [e for e in events if e.phase == "flow_start"]
    assert starts[-1].depth == 2
    assert starts[-1].label == "创建本步录制记录"


def test_partial_initialization_keeps_unexecuted_parts_unknown():
    with record_timing(True) as timing:
        with flow_scope("1.2", part="1.2.1"):
            pass
        with pytest.raises(ValueError), flow_scope("1.2", part="1.2.2"):
            raise ValueError("PRIVATE dependency error")
    row = next(r for r in timing.snapshot()["phases"][0]["substeps"] if r["id"] == "1.2")
    assert row["count"] == 2 and row["raised"] == 1
    assert row["parts"][1]["raised"] == 1
    assert row["parts"][2]["duration_sec"] is None
    assert row["parts"][2]["measurement"] == "not_observed"
    assert "PRIVATE" not in str(row)


def test_part_totals_stay_complete_when_step_details_are_capped():
    with record_timing(True, max_steps=1) as timing:
        record_phase("steps")
        for step in (1, 2):
            with record_step(step), flow_scope("2.6", part="2.6.1"):
                pass
    data = timing.snapshot()
    row = next(r for r in data["phases"][1]["substeps"] if r["id"] == "2.6")
    assert row["parts"][0]["count"] == 2 and len(data["steps"]) == 1
    assert data["step_details_complete"] is False
