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


def event(node, action, state="unknown", ordinal=1, execution="e", result="succeeded"):
    return VerifiedEvent(
        node_index=node,
        tool_ordinal=ordinal,
        execution_id=execution,
        action=action,
        observed_state=state,
        action_result=result if action != "probe" else "unattributed",
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


def ranges(result):
    assert result.unknowns == ()
    return [(i.start_inclusive, i.end_exclusive) for i in result.intervals]


def test_launch_without_any_probe_extends_through_file_only_tail():
    result = lifecycle(event(1, "start"), end=3)
    assert ranges(result) == [(1, 4)]
    assert result.intervals[0].probe_event is None
    assert result.intervals[0].open_end


def test_stop_success_closes_half_open_boundary():
    result = lifecycle(event(1, "start"), event(3, "stop"), end=3)
    assert ranges(result) == [(1, 3)]
    assert result.intervals[0].stop_event.node_index == 3


def test_output_only_start_hint_does_not_preempt_a_recorded_start_command():
    hint = event(1, "start", "up").model_copy(update={"request_span": None, "executable": False})
    assert ranges(lifecycle(hint, event(2, "start", result="failed"), end=3)) == [(2, 4)]


@pytest.mark.parametrize("result", ["failed", "unattributed"])
def test_start_opens_interval_regardless_of_result_but_only_successful_stop_closes_it(result):
    assert ranges(lifecycle(event(1, "start", result=result), end=3)) == [(1, 4)]
    assert ranges(lifecycle(event(1, "start", result=result), event(3, "stop"), end=3)) == [(1, 3)]
    observed = lifecycle(event(1, "start"), event(2, "start", result=result),
                         event(3, "stop", result=result), end=3)
    assert ranges(observed) == [(1, 4)]


@pytest.mark.parametrize("state", ["up", "down", "unknown"])
def test_probe_cannot_open_close_or_shift_a_launch_interval(state):
    assert ranges(lifecycle(event(2, "probe", state), end=3)) == []
    assert ranges(lifecycle(event(1, "start"), event(2, "probe", state), end=3)) == [(1, 4)]
    assert ranges(lifecycle(event(1, "start"), event(2, "stop"),
                            event(3, "probe", state), end=3)) == [(1, 2)]


def test_delayed_readiness_does_not_delay_launch_intent():
    result = lifecycle(event(5, "start"), event(8, "probe", "up"), event(10, "stop"))
    assert ranges(result) == [(5, 10)]
    assert result.intervals[0].probe_event.node_index == 8


def test_repeated_successful_start_keeps_boolean_state_and_original_recipe():
    result = lifecycle(event(1, "start", execution="first"), event(2, "start", execution="second"),
                       event(3, "stop"), end=3)
    assert ranges(result) == [(1, 3)]
    assert result.intervals[0].start_event.execution_id == "first"


def test_same_node_tools_settle_in_original_call_order_even_if_supplied_reversed():
    result = lifecycle(event(1, "stop", ordinal=2), event(1, "start", ordinal=1), end=3)
    assert ranges(result) == []
    with pytest.raises(ValueError, match="no matching recorded start"):
        lifecycle(event(1, "start", ordinal=2), event(1, "stop", ordinal=1), end=3)


def test_compound_actions_settle_by_verified_fragment_offset():
    start = event(1, "start").model_copy(update={"request_span": span("request.command", "start")})
    stop = event(1, "stop").model_copy(update={"request_span": span("request.command", "stop").model_copy(
        update={"start": 10, "end": 14})})
    assert ranges(lifecycle(stop, start, end=3)) == []


def test_stop_then_restart_at_same_node_keeps_nonoverlapping_intervals():
    result = lifecycle(event(1, "start"), event(3, "stop"),
                       event(3, "start", ordinal=2), end=3)
    assert ranges(result) == [(1, 3), (3, 4)]


def test_empty_events_and_invalid_ranges():
    assert ranges(lifecycle()) == []
    with pytest.raises(ValueError, match="must not follow"):
        analyze_lifecycles((service("A"),), 5, 4)
    with pytest.raises(ValueError, match="outside"):
        lifecycle(event(5, "start"), end=3)
