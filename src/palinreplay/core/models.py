"""Versioned Record facts, independent of Agent, model and Sandbox implementations."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Identity = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
Counter = Annotated[int, Field(strict=True, ge=0)]
EventKind = Literal["dispatched", "result", "delivered", "accepted"]
Outcome = Literal["returned", "error", "unknown"]


def new_id() -> str:
    return uuid4().hex


class RecordModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class ArtifactRef(RecordModel):
    sha256: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")
    size: Counter
    media_type: str = Field(default="application/octet-stream", min_length=1)


class Fact(RecordModel):
    schema_version: Literal["palinreplay.record.v1"] = "palinreplay.record.v1"
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class Run(Fact):
    run_id: Identity = Field(default_factory=new_id)
    task_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    # Caller supplies public configuration, never credentials or SDK objects.
    config: ArtifactRef
    source_node_id: Identity | None = None


class Attempt(Fact):
    attempt_id: Identity = Field(default_factory=new_id)
    run_id: Identity
    previous_attempt_id: Identity | None = None


class Step(Fact):
    step_id: Identity = Field(default_factory=new_id)
    run_id: Identity
    attempt_id: Identity
    index: Annotated[int, Field(strict=True, ge=1)]
    parent_node_id: Identity


class Operation(Fact):
    """One logical call; retries retain separate Execution identities."""

    operation_id: Identity = Field(default_factory=new_id)
    run_id: Identity
    step_id: Identity
    kind: Literal["model", "tool"]
    name: str = Field(min_length=1)
    tool_call_id: str | None = Field(default=None, min_length=1)
    caused_by_execution_id: Identity | None = None


class Execution(Fact):
    execution_id: Identity = Field(default_factory=new_id)
    run_id: Identity
    operation_id: Identity
    index: Annotated[int, Field(strict=True, ge=1)]
    request: ArtifactRef
    context: ArtifactRef | None = None
    visibility: Literal["observed-attempt", "opaque-retries"] = "observed-attempt"


class ToolGroup(Fact):
    """Adapter-declared whole group, durable before any tool is prepared."""

    toolgroup_id: Identity = Field(default_factory=new_id)
    run_id: Identity
    step_id: Identity
    model_execution_id: Identity
    tool_call_ids: tuple[Annotated[str, Field(strict=True, min_length=1)], ...]

    @model_validator(mode="after")
    def validate_group(self) -> "ToolGroup":
        if len(set(self.tool_call_ids)) != len(self.tool_call_ids):
            raise ValueError("duplicate provider tool-call identities in group")
        return self


class StepCalls(RecordModel):
    """Validated call selection at a complete step boundary."""

    model_execution_id: Identity
    tool_operation_ids: tuple[Identity, ...]
    tool_execution_ids: tuple[Identity, ...]
    accepted_event_sequence: Counter


class Event(Fact):
    event_id: Identity = Field(default_factory=new_id)
    run_id: Identity
    execution_id: Identity
    sequence: Annotated[int, Field(strict=True, ge=1)]
    kind: EventKind
    payload: ArtifactRef | None = None
    details: ArtifactRef | None = None
    outcome: Outcome | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "Event":
        if (self.payload is not None) != (self.kind != "dispatched"):
            raise ValueError("result, delivered and accepted require payload; dispatched does not")
        if (self.outcome is not None) != (self.kind == "result"):
            raise ValueError("only result events require an outcome")
        return self


class CaptureInfo(RecordModel):
    scope: str = Field(min_length=1)
    included: tuple[str, ...] = Field(min_length=1)
    excluded: tuple[str, ...] = ()
    attributes: tuple[str, ...] = Field(min_length=1)
    consistency: Literal["step-boundary-nonatomic", "fixture"]
    limitations: tuple[str, ...] = ()
    complete: bool = Field(strict=True)
    missing: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_completeness(self) -> "CaptureInfo":
        if self.complete and self.missing:
            raise ValueError("a capture with missing material cannot be complete")
        return self


class State(Fact):
    """Full logical state entrance; storage does not interpret backend manifests."""

    state_id: Identity = Field(default_factory=new_id)
    backend: str = Field(min_length=1)
    capture: CaptureInfo
    manifest: ArtifactRef
    runtime: ArtifactRef
    artifacts: tuple[ArtifactRef, ...] = Field(min_length=1)
    # These three relations are separate: logical basis, physical dependencies,
    # and Node.parent_node_id (execution history).
    logical_parent_id: Identity | None = None
    dependencies: tuple[Identity, ...] = ()

    @model_validator(mode="after")
    def validate_dependencies(self) -> "State":
        if self.state_id in self.dependencies or self.logical_parent_id == self.state_id:
            raise ValueError("state cannot refer to itself")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("duplicate physical state dependencies")
        return self


class StateCapture(Fact):
    """Capture evidence for a proposed boundary, distinct from shared file state."""

    statecapture_id: Identity = Field(default_factory=new_id)
    run_id: Identity
    attempt_id: Identity
    step_id: Identity | None
    node_id: Identity
    index: Counter
    state_id: Identity
    details: ArtifactRef


class Node(Fact):
    node_id: Identity = Field(default_factory=new_id)
    run_id: Identity
    attempt_id: Identity
    index: Counter
    parent_node_id: Identity | None = None
    step_id: Identity | None = None
    context: ArtifactRef
    input_position: ArtifactRef
    state_id: Identity
    accepted_event_sequence: Counter
    model_execution_id: Identity | None = None
    # Adapter declares the whole expected tool group, including calls for which
    # no Execution has yet been recorded; publication must reject such gaps.
    tool_operation_ids: tuple[Identity, ...] = ()
    tool_execution_ids: tuple[Identity, ...] = ()

    @model_validator(mode="after")
    def validate_boundary(self) -> "Node":
        if self.index == 0:
            if (
                self.parent_node_id is not None
                or self.step_id is not None
                or self.model_execution_id is not None
                or self.tool_operation_ids
                or self.tool_execution_ids
                or self.accepted_event_sequence
            ):
                raise ValueError("C0 cannot contain step, parent or accepted-call history")
        elif not self.parent_node_id or not self.step_id or not self.model_execution_id:
            raise ValueError("step node requires parent, step and model execution")
        for values in (self.tool_operation_ids, self.tool_execution_ids):
            if len(set(values)) != len(values):
                raise ValueError("duplicate tool identities in node")
        if len(self.tool_operation_ids) != len(self.tool_execution_ids):
            raise ValueError("each declared tool operation requires an execution")
        return self


class Failure(Fact):
    failure_id: Identity = Field(default_factory=new_id)
    run_id: Identity
    attempt_id: Identity
    step_id: Identity | None = None
    execution_id: Identity | None = None
    category: Literal["execution", "recording", "cleanup"]
    details: ArtifactRef


class CallEvidence(Fact):
    """Observed internal I/O within an Execution, not an inferred HTTP attempt."""

    callevidence_id: Identity = Field(default_factory=new_id)
    run_id: Identity
    execution_id: Identity
    index: int = Field(ge=1, strict=True)
    kind: Literal["request", "response", "error"]
    payload: ArtifactRef


class RunEnd(Fact):
    """End of one live attempt; this does not claim task success or Resume support."""

    runend_id: Identity = Field(default_factory=new_id)
    run_id: Identity
    attempt_id: Identity
    status: Literal["completed", "paused", "cancelled", "failed", "incomplete"]
    last_node_id: Identity | None
    details: ArtifactRef


class IntegrityIssue(RecordModel):
    record: str
    message: str


class IntegrityReport(RecordModel):
    issues: tuple[IntegrityIssue, ...]
    incomplete_steps: tuple[str, ...]
    pending_executions: tuple[str, ...]
    staged_artifacts: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Intact unfinished history is not corruption; inspect the tail separately."""
        return not self.issues
