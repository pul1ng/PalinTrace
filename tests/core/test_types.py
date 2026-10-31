import json

import pytest
from pydantic import ValidationError

from palinreplay.core.models import ArtifactRef, CaptureInfo, Event, Node, Run, State


def ref():
    return ArtifactRef(sha256="a" * 64, size=3)


def test_versioned_facts_roundtrip_with_aware_time_and_immutable_fields():
    run = Run(task_id="example", agent_name="fake", agent_version="fixed", config=ref())
    assert Run.model_validate_json(run.model_dump_json()) == run
    assert run.created_at.tzinfo is not None
    with pytest.raises(ValidationError):
        run.agent_name = "changed"
    body = json.loads(run.model_dump_json())
    body["schema_version"] = "palinreplay.record.v999"
    with pytest.raises(ValidationError):
        Run.model_validate(body)
    body["schema_version"] = "palinreplay.record.v1"
    body["created_at"] = "2026-10-22T12:00:00"
    with pytest.raises(ValidationError):
        Run.model_validate(body)


@pytest.mark.parametrize("update", [{"run_id": "../escape"}, {"run_id": ""}, {"surprise": 1}])
def test_record_rejects_unsafe_identity_or_unknown_field(update):
    with pytest.raises(ValidationError):
        Run(task_id="example", agent_name="fake", agent_version="fixed", config=ref(), **update)


@pytest.mark.parametrize("size", [-1, True, "12"])
def test_artifact_size_is_a_nonnegative_integer(size):
    with pytest.raises(ValidationError):
        ArtifactRef(sha256="a" * 64, size=size)


def test_event_payload_and_outcome_are_separate_from_acceptance():
    fields = {"run_id": "r", "execution_id": "e", "sequence": 1}
    Event(**fields, kind="dispatched")
    Event(**fields, kind="result", payload=ref(), outcome="error")
    Event(**fields, kind="accepted", payload=ref())
    with pytest.raises(ValidationError):
        Event(**fields, kind="result", payload=ref())
    with pytest.raises(ValidationError):
        Event(**fields, kind="accepted", payload=ref(), outcome="returned")
    with pytest.raises(ValidationError):
        Event(**fields, kind="delivered")


def test_c0_and_complete_step_node_have_different_required_boundaries():
    fields = {
        "run_id": "r",
        "attempt_id": "a",
        "state_id": "s",
        "context": ref(),
        "input_position": ref(),
        "accepted_event_sequence": 0,
    }
    Node(**fields, index=0)
    with pytest.raises(ValidationError):
        Node(**fields, index=1)
    with pytest.raises(ValidationError):
        Node(**fields, index=0, step_id="step")
    with pytest.raises(ValidationError):
        Node(
            **fields,
            index=1,
            parent_node_id="c0",
            step_id="step",
            model_execution_id="e",
            tool_operation_ids=("tool", "tool"),
            tool_execution_ids=("e1", "e2"),
        )


def test_incomplete_capture_and_physical_dependencies_are_explicit():
    capture = CaptureInfo(
        scope="fixture",
        included=("/",),
        attributes=("content",),
        consistency="fixture",
        complete=False,
        missing=("/data",),
    )
    state = State(
        backend="fake", capture=capture, manifest=ref(), runtime=ref(), artifacts=(ref(),)
    )
    assert not state.capture.complete
    with pytest.raises(ValidationError):
        CaptureInfo.model_validate({**capture.model_dump(), "complete": True})
    with pytest.raises(ValidationError):
        State.model_validate({**state.model_dump(), "dependencies": (state.state_id,)})


def test_model_copy_cannot_bypass_validation_when_persisted():
    invalid = ref().model_copy(update={"size": -1})
    with pytest.raises(ValidationError):
        Run(task_id="task", agent_name="fake", agent_version="v1", config=invalid)
