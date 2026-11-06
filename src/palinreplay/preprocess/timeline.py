"""Lossless tool timeline for one pinned Run."""

import math
from typing import Literal

from pydantic import Field, StrictInt, StrictStr, field_validator

from palinreplay.core.errors import IntegrityError, RecordUsageError
from palinreplay.core.models import ArtifactRef, Event
from palinreplay.core.schema import Spec
from palinreplay.preprocess.source import PinnedSource
from palinreplay.record.calls import CallTrace, read_call
from palinreplay.storage.store import RecordStore


class TimelineRequest(Spec):
    command: StrictStr
    interpreter: tuple[StrictStr, ...]
    cwd: StrictStr
    env: dict[StrictStr, StrictStr]
    timeout_sec: int | float

    @field_validator("timeout_sec")
    @classmethod
    def positive_finite(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("timeout_sec must be a number")
        if not math.isfinite(value) or value <= 0:
            raise ValueError("timeout_sec must be positive and finite")
        return value

    @field_validator("interpreter")
    @classmethod
    def nonempty_interpreter(cls, value):
        if not value or any(not item or "\x00" in item for item in value):
            raise ValueError("interpreter must contain nonempty arguments without NUL")
        return value

    @field_validator("command", "cwd")
    @classmethod
    def no_nul(cls, value):
        if "\x00" in value:
            raise ValueError("command and cwd must not contain NUL")
        return value


class TimelineResult(Spec):
    outcome: Literal["returned", "error", "unknown"]
    exit_code: StrictInt | None
    output_text: StrictStr | None


class TimelineRefs(Spec):
    request: ArtifactRef
    output: ArtifactRef
    details: ArtifactRef


class TimelineCall(Spec):
    node_index: StrictInt = Field(ge=0)
    tool_ordinal: StrictInt = Field(ge=1)
    execution_id: StrictStr
    tool_name: StrictStr
    request: TimelineRequest
    result: TimelineResult
    refs: TimelineRefs


class TimelineSource(Spec):
    run_id: StrictStr
    start_index: StrictInt = Field(ge=0)
    end_index: StrictInt = Field(ge=0)
    digest_scheme: StrictStr
    selection_sha256: StrictStr


class Timeline(Spec):
    source: TimelineSource
    calls: tuple[TimelineCall, ...]


def _request(store: RecordStore, trace: CallTrace) -> TimelineRequest:
    where = f"node/tool execution {trace.execution.execution_id}"
    value = store.read_json(trace.execution.request)
    if not isinstance(value, dict):
        raise RecordUsageError(f"{where} has an unsupported tool request: expected an object")
    action = value.get("action")
    command = action.get("command") if isinstance(action, dict) else None
    if not isinstance(command, str):
        raise RecordUsageError(f"{where} has an unsupported tool request: no string action.command")
    try:
        return TimelineRequest(
            command=command,
            interpreter=value.get("interpreter"),
            cwd=value.get("cwd"),
            env=value.get("env"),
            timeout_sec=value.get("timeout_sec"),
        )
    except ValueError as error:
        raise RecordUsageError(f"{where} has an unsupported tool request: {error}") from error


def _result(store: RecordStore, trace: CallTrace) -> tuple[TimelineResult, TimelineRefs]:
    results = [event for event in trace.events if event.kind == "result"]
    if len(results) != 1:
        raise IntegrityError(
            f"tool execution {trace.execution.execution_id} must have exactly one result event"
        )
    event: Event = results[0]
    if event.details is None or event.payload is None:
        raise IntegrityError(
            f"tool execution {trace.execution.execution_id} lacks result materials"
        )
    details = store.read_json(event.details)
    decoded = details.get("backend", {}).get("decoded") if isinstance(details, dict) else None
    exit_code = decoded.get("returncode") if isinstance(decoded, dict) else None
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        exit_code = None
    output = store.read_payload(event.payload).data
    try:
        output_text = output.decode("utf-8")
    except UnicodeDecodeError:
        output_text = None
    result = TimelineResult(
        outcome=event.outcome,
        exit_code=exit_code,
        output_text=output_text,
    )
    refs = TimelineRefs(request=trace.execution.request, output=event.payload, details=event.details)
    return result, refs


def read_timeline(store: RecordStore, source: PinnedSource) -> Timeline:
    """Read complete, verified tool facts in committed source order.

    Tool rows are defined by committed ``tool_execution_ids`` and the recorded
    operation, not by accepted message roles: the final C40 submission still
    contains a real tool execution.
    """
    calls = []
    run_id = source.run.run_id
    for node in source.nodes:
        if len(node.tool_operation_ids) != len(node.tool_execution_ids):
            raise IntegrityError(f"C{node.index} has an incomplete declared tool group")
        for ordinal, execution_id in enumerate(node.tool_execution_ids, 1):
            trace = read_call(store, execution_id)
            if trace.operation.kind != "tool":
                raise IntegrityError(f"C{node.index} references a non-tool execution {execution_id}")
            if (
                trace.operation.run_id != run_id
                or trace.execution.run_id != run_id
                or trace.operation.operation_id != node.tool_operation_ids[ordinal - 1]
                or trace.execution.operation_id != trace.operation.operation_id
            ):
                raise IntegrityError(f"C{node.index} tool execution does not belong to its group")
            if not trace.events or trace.events[-1].sequence > node.accepted_event_sequence:
                raise IntegrityError(f"C{node.index} tool execution is not accepted at this node")
            request = _request(store, trace)
            result, refs = _result(store, trace)
            calls.append(
                TimelineCall(
                    node_index=node.index,
                    tool_ordinal=ordinal,
                    execution_id=execution_id,
                    tool_name=trace.operation.name,
                    request=request,
                    result=result,
                    refs=refs,
                )
            )
    timeline = Timeline(source=TimelineSource.model_validate(source.document()), calls=tuple(calls))
    for sequence, call in enumerate(timeline.calls):
        if sequence and _order_key(call) <= _order_key(timeline.calls[sequence - 1]):
            raise IntegrityError("tool timeline is not in committed node/tool order")
    return timeline


def _order_key(call: TimelineCall) -> tuple[int, int]:
    return call.node_index, call.tool_ordinal
