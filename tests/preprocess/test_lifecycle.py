import pytest

from palinreplay.preprocess.candidates import Candidate
from palinreplay.preprocess.evidence import (
    CandidateEvidence,
    ServiceEvidence,
    VerifiedEvent,
    VerifiedSpan,
)
from palinreplay.preprocess.lifecycle import analyze_lifecycles


def span(field, quote="x"):
    return VerifiedSpan(
        field=field, quote=quote, start=0, end=len(quote), artifact_sha256="1" * 64
    )


def event(node, action, state, ordinal=1, execution="e"):
    return VerifiedEvent(
        node_index=node,
        tool_ordinal=ordinal,
        execution_id=execution,
        action=action,
        observed_state=state,
        request_span=span("request.command") if action in ("start", "probe", "stop") else None,
        output_span=span("result.output_text") if state != "unknown" else None,
        executable=action in ("start", "probe", "stop"),
        dependencies=(),
        reason="fixture",
    )


def service(name, *events):
    item = CandidateEvidence(
        candidate=Candidate(
            name=name,
            node_index=events[0].node_index if events else 1,
            execution_id=events[0].execution_id if events else "e",
            actions=tuple(sorted({event.action for event in events})),
            candidate_state="unknown",
        ),
        identity=(),
        events=tuple(events),
        unknowns=(),
    )
    return ServiceEvidence(
        name=name,
        aliases=(name,),
        identity_keys=(),
        items=(item,),
        identity_unknown=False,
        unknowns=(),
    )


def lifecycle(*events, end=40, start=0):
    return analyze_lifecycles((service("A", *events),), start, end)[0]


def test_expected_fixture_lifecycle_is_half_open_and_has_no_unknowns():
    result = lifecycle(
        event(1, "probe", "down"),
        event(5, "start", "up"),
        event(6, "probe", "up"),
        event(25, "stop", "down"),
    )
    assert [(i.start_inclusive, i.end_exclusive) for i in result.intervals] == [(5, 25)]
    interval = result.intervals[0]
    assert interval.start_event.node_index == 5
    assert interval.probe_event.node_index == 6
    assert interval.stop_event.node_index == 25
    assert interval.open_end is False
    assert result.unknowns == ()


def test_delayed_readiness_starts_at_readiness_but_keeps_launch_evidence():
    result = lifecycle(
        event(5, "start", "unknown"),
        event(8, "probe", "up"),
        event(10, "stop", "down"),
    )
    assert [(i.start_inclusive, i.end_exclusive) for i in result.intervals] == [(8, 10)]
    assert result.intervals[0].start_event.node_index == 5
    assert [(u.start_inclusive, u.end_exclusive, u.reason) for u in result.unknowns] == [
        (5, 8, "service state was unknown before the next live observation")
    ]


def test_missing_tail_ends_at_last_live_plus_one_and_reports_unknown_remainder():
    result = lifecycle(event(5, "start", "up"), event(6, "probe", "up"))
    assert [(i.start_inclusive, i.end_exclusive) for i in result.intervals] == [(5, 7)]
    assert [(u.start_inclusive, u.end_exclusive) for u in result.unknowns] == [(7, 41)]


def test_live_at_last_node_extends_to_end_plus_one_without_stop_evidence():
    result = lifecycle(event(39, "start", "up"), event(40, "probe", "up"))
    interval, = result.intervals
    assert (interval.start_inclusive, interval.end_exclusive, interval.open_end) == (39, 41, True)
    assert result.unknowns == ()


def test_unknown_stop_breaks_continuity_and_later_live_observation_starts_new_interval():
    result = lifecycle(
        event(5, "start", "up"),
        event(10, "stop", "unknown"),
        event(12, "probe", "up"),
        event(20, "stop", "down"),
    )
    assert [(i.start_inclusive, i.end_exclusive) for i in result.intervals] == [(5, 10), (12, 20)]
    assert [(u.start_inclusive, u.end_exclusive) for u in result.unknowns] == [(10, 12)]


def test_successful_restart_splits_lifecycle_at_the_start_node():
    result = lifecycle(
        event(5, "start", "up"),
        event(10, "start", "up"),
        event(20, "stop", "down"),
    )
    assert [(i.start_inclusive, i.end_exclusive) for i in result.intervals] == [(5, 10), (10, 20)]


def test_same_node_start_then_stop_has_no_confirmed_interval():
    result = lifecycle(event(5, "start", "up"), event(5, "stop", "down", ordinal=2))
    assert result.intervals == ()
    assert result.unknowns == ()


def test_down_then_up_same_node_starts_interval_at_that_node():
    result = lifecycle(
        event(5, "stop", "down"),
        event(5, "start", "up", ordinal=2),
        event(8, "stop", "down"),
    )
    assert [(i.start_inclusive, i.end_exclusive) for i in result.intervals] == [(5, 8)]


def test_start_only_is_unknown_not_no_service():
    result = lifecycle(event(5, "start", "unknown"))
    assert result.intervals == ()
    assert [(u.start_inclusive, u.end_exclusive) for u in result.unknowns] == [(5, 41)]


def test_down_only_is_confidently_absent():
    result = lifecycle(event(5, "probe", "down"))
    assert result.intervals == () and result.unknowns == ()


def test_empty_events_and_invalid_range():
    result = lifecycle()
    assert result.intervals == () and result.unknowns == ()
    with pytest.raises(ValueError, match="must not follow"):
        analyze_lifecycles((service("A"),), 5, 4)
