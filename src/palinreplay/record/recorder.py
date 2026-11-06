"""Coordinate C0, complete steps and termination without owning the Agent loop.

The caller owns the store and upstream execution. All calls use the current
RecordStep; capture closes those entry points and never retries side effects.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from palinreplay.core.interfaces import AgentSnapshot, BoundaryAdapter, CaptureBoundary, Payload
from palinreplay.core.models import (
    Attempt,
    Failure,
    Node,
    Operation,
    Run,
    RunEnd,
    State,
    Step,
    StepCalls,
    ToolGroup,
    new_id,
)
from palinreplay.diagnostics.metrics import measure, phase
from palinreplay.diagnostics.record_timing import flow_scope, record_step
from palinreplay.record.calls import (
    RecordedCall,
    RecordingError,
    RecordingPorts,
    RecordingStoppedError,
    _error_details,
    _payload,
    _resolved,
    _synchronous,
)
from palinreplay.storage.store import ConflictError, IncompleteRecordError, RecordStore


class RecordStep(RecordingPorts):
    """Managed synchronous ports; a whole tool group must be declared explicitly."""

    def __init__(self, owner: Recorder, step: Step):
        super().__init__(owner.store, step.step_id)
        self._owner = owner
        self._group: ToolGroup | None = None

    def _ready(self) -> None:
        super()._ready()
        if self._owner._phase != "step" or self._owner._active is not self:
            raise RecordingStoppedError("step calls are closed outside the active execution phase")

    def _prepare(self, kind, adapter, request, **kwargs):
        self._ready()
        operation = kwargs.get("operation")
        if kind == "model":
            models = [
                o
                for o in self.store.list(Operation, run_id=self.step.run_id)
                if o.step_id == self.step.step_id and o.kind == "model"
            ]
            if self._group is not None or (models and operation is None):
                raise ConflictError(
                    "one model operation per step; use explicit retry for known errors"
                )
        else:
            if (
                self._group is None
                or kwargs.get("tool_call_id", getattr(operation, "tool_call_id", None))
                not in self._group.tool_call_ids
            ):
                raise ConflictError("tool must belong to the previously declared whole group")
            cause = kwargs.get("caused_by_execution_id")
            if cause is not None and cause != self._group.model_execution_id:
                raise ConflictError("tool cause differs from the declared model response")
            kwargs["caused_by_execution_id"] = self._group.model_execution_id
        return super()._prepare(kind, adapter, request, **kwargs)

    def _abort(self, *args, **kwargs) -> None:
        try:
            super()._abort(*args, **kwargs)
        except BaseException as error:
            self._owner._stop_after_error(error)
            raise

    def declare_tools(self, model: RecordedCall, tool_call_ids: tuple[str, ...]) -> ToolGroup:
        """Seal the Agent-observed answer's complete group, including an explicit ()."""
        self._ready()
        if model._ports is not self or model.operation.kind != "model":
            raise ValueError("model call must belong to this step")
        if self._group is not None:
            raise ConflictError("tool group has already been declared")
        group = ToolGroup(
            run_id=self.step.run_id,
            step_id=self.step.step_id,
            model_execution_id=model.execution_id,
            tool_call_ids=tool_call_ids,
        )
        self._busy = True
        try:
            self._group = self.store.save(group)
            return self._group
        except BaseException as error:
            self._owner._abort("tool-group", error)
        finally:
            self._busy = False


class Recorder:
    """One new run/attempt, one Agent and one state backend, on the creating thread.

    start() publishes C0 before begin_step() exposes calls. commit_step() is an
    explicit completion assertion: a missing group/result/adoption is fatal to
    this recorder, leaving the old prefix readable. No automatic recovery,
    Agent scheduling, Docker capture, parallel or streaming adaptation occurs.
    """

    def __init__(
        self,
        store: RecordStore,
        *,
        task_id: str,
        agent_name: str,
        agent_version: str,
        config: Payload,
        boundary: BoundaryAdapter,
        cleanup: Callable[[], None] | None = None,
    ):
        _synchronous(boundary.export_agent, "Agent snapshot exporter")
        _synchronous(boundary.capture_state, "state capture backend")
        if cleanup is not None:
            _synchronous(cleanup, "resource cleanup")
        config = _payload(config)
        self.store, self.boundary, self._cleanup = store, boundary, cleanup
        self._thread = threading.get_ident()
        self._phase = "new"
        self._active: RecordStep | None = None
        self._head: Node | None = None
        self._end: RunEnd | None = None
        self.last_boundary_metrics: dict | None = None
        self.run = store.save(
            Run(
                task_id=task_id,
                agent_name=agent_name,
                agent_version=agent_version,
                config=store.put_bytes(config.data, media_type=config.media_type),
            )
        )
        self.attempt = store.save(Attempt(run_id=self.run.run_id))

    @property
    def current_node(self) -> Node | None:
        return self._head

    @property
    def end(self) -> RunEnd | None:
        return self._end

    @property
    def phase(self) -> str:
        return self._phase

    def _control_ready(self) -> None:
        if threading.get_ident() != self._thread:
            raise RuntimeError("Recorder is synchronous and thread-confined")
        if self._phase in ("capturing", "starting") or (self._active and self._active._busy):
            raise RuntimeError("reentrant Recorder lifecycle changes are unsupported")

    def _require(self, phase: str) -> None:
        self._control_ready()
        if self._phase != phase:
            raise RecordingStoppedError(f"expected Recorder phase {phase}, got {self._phase}")

    def _failure(self, category: str, phase: str, error: BaseException) -> None:
        self.store.save(
            Failure(
                run_id=self.run.run_id,
                attempt_id=self.attempt.attempt_id,
                step_id=self._active.step.step_id if self._active else None,
                category=category,
                details=self.store.put_json({"phase": phase, "error": _error_details(error)}),
            )
        )

    def _write_end(self, status: str, details: dict) -> RunEnd:
        self._end = self.store.save(
            RunEnd(
                run_id=self.run.run_id,
                attempt_id=self.attempt.attempt_id,
                status=status,
                last_node_id=self._head.node_id if self._head else None,
                details=self.store.put_json(details),
            )
        )
        return self._end

    def _stop_after_error(self, error: BaseException) -> None:
        self._phase = "stopped"
        if self._end is None:
            try:
                # Publication may have succeeded before commit_node raised.
                self._head = max(self.store.list(Node, run_id=self.run.run_id),
                                 key=lambda node: node.index, default=None)
                self._write_end(
                    "cancelled" if not isinstance(error, Exception) else "failed",
                    {"error": _error_details(error)},
                )
            except BaseException as diagnostic:
                error.add_note(f"attempt termination could not be persisted: {diagnostic!r}")

    def _abort(self, phase: str, error: BaseException) -> None:
        self._phase = "stopped"
        failure = RecordingError(phase, error, execution_id=None)
        try:
            self._failure("recording", phase, error)
        except BaseException as diagnostic:
            failure.diagnostic_error = diagnostic
        self._stop_after_error(error)
        if not isinstance(error, Exception):
            error.add_note(str(failure))
            raise error from failure
        raise failure from error

    def _agent_snapshot(self, boundary: CaptureBoundary) -> AgentSnapshot:
        snapshot = _resolved(self.boundary.export_agent(boundary))
        if not isinstance(snapshot, AgentSnapshot):
            raise TypeError("Agent exporter must return AgentSnapshot")
        return snapshot

    def _capture(self, calls: StepCalls | None) -> Node:
        index = self._active.step.index if self._active else 0
        with (record_step(index), flow_scope("1.6" if calls is None else "2.7", step=index),
              measure() as metrics, self.store._verification_scope()):
            try:
                with phase("boundary.capture_commit"):
                    return self._capture_boundary(calls)
            finally:
                self.last_boundary_metrics = metrics.snapshot()

    def _capture_boundary(self, calls: StepCalls | None) -> Node:
        self._phase = "capturing"
        step = self._active.step if self._active else None
        boundary = CaptureBoundary(
            node_id=new_id(),
            run_id=self.run.run_id,
            attempt_id=self.attempt.attempt_id,
            index=step.index if step else 0,
            step_id=step.step_id if step else None,
            parent_node_id=self._head.node_id if self._head else None,
            accepted_event_sequence=calls.accepted_event_sequence if calls else 0,
        )
        phase = "agent-capture"
        try:
            with flow_scope("S1", step=boundary.index):
                before = self._agent_snapshot(boundary)
                previous = self.store.get(State, self._head.state_id) if self._head else None
            phase = "state-capture"
            with flow_scope("S2", step=boundary.index):
                state = _resolved(self.boundary.capture_state(self.store, boundary, previous))
            if not isinstance(state, State):
                raise TypeError("state backend must return State with durable materials")
            with flow_scope("S4", step=boundary.index, part="S4.4"):
                state = self.store.save(state)
            phase = "boundary-check"
            with flow_scope("S5", step=boundary.index):
                after = self._agent_snapshot(boundary)
                if before != after:
                    raise IncompleteRecordError("Agent context/input position changed during capture")
                if calls is not None and self.store.accepted_step(step.step_id) != calls:
                    raise IncompleteRecordError("call facts changed during boundary capture")
            phase = "node-commit"
            with flow_scope("S6", step=boundary.index):
                node = self.store.commit_node(
                    Node(
                        node_id=boundary.node_id,
                        run_id=boundary.run_id,
                        attempt_id=boundary.attempt_id,
                        index=boundary.index,
                        step_id=boundary.step_id,
                        parent_node_id=boundary.parent_node_id,
                        context=self.store.put_bytes(
                            before.context.data, media_type=before.context.media_type
                        ),
                        input_position=self.store.put_bytes(
                            before.input_position.data, media_type=before.input_position.media_type
                        ),
                        state_id=state.state_id,
                        **(calls.model_dump() if calls else {"accepted_event_sequence": 0}),
                    )
                )
        except BaseException as error:
            self._abort(phase, error)
        self._head, self._active, self._phase = node, None, "ready"
        return node

    def start(self) -> Node:
        self._require("new")
        if self.store.recording_source(self.run.run_id) is not None:
            raise IncompleteRecordError("source-reference recording must use start_from_source()")
        return self._capture(None)

    def start_from_source(self) -> None:
        """Enter a restored external prefix without capturing a local initial boundary."""
        self._require("new")
        if self.store.recording_source(self.run.run_id) is None:
            raise IncompleteRecordError("source initialization requires a valid source reference")
        self._phase = "ready"

    def begin_step(self) -> RecordStep:
        self._require("ready")
        try:
            source = self.store.recording_source(self.run.run_id) if self._head is None else None
            step = self.store.save(
                Step(
                    run_id=self.run.run_id,
                    attempt_id=self.attempt.attempt_id,
                    index=self._head.index + 1 if self._head else source.start.index + 1,
                    parent_node_id=self._head.node_id if self._head else None,
                )
            )
            self._active = RecordStep(self, step)
        except BaseException as error:
            self._abort("begin-step", error)
        self._phase = "step"
        return self._active

    def commit_step(self) -> Node:
        self._require("step")
        try:
            with flow_scope("2.6", step=self._active.step.index, part="2.6.2"):
                self._active._ready()
                calls = self.store.accepted_step(self._active.step.step_id)
        except BaseException as error:
            self._abort("step-completeness", error)
        return self._capture(calls)

    def _terminate(self, status: str, reason: str, details: dict | None = None) -> RunEnd:
        self._phase = "stopped"
        try:
            return self._write_end(status, {**(details or {}), "reason": reason})
        except BaseException as error:
            self._abort("termination", error)

    def finish(self, reason: str = "Agent finished", *, details: dict | None = None) -> RunEnd:
        """End at a committed boundary; completion says nothing about solving the task."""
        self._require("ready")
        return self._terminate("completed", reason, details)

    def pause(
        self, reason: str = "caller paused at a boundary", *, details: dict | None = None
    ) -> RunEnd:
        self._require("ready")
        return self._terminate("paused", reason, details)

    def cancel(self, reason: str = "caller cancelled", *, details: dict | None = None) -> RunEnd:
        self._control_ready()
        if self._phase not in ("new", "ready", "step"):
            raise RecordingStoppedError("Recorder is already stopped")
        return self._terminate("cancelled", reason, details)

    def end_incomplete(self, reason: str, *, details: dict | None = None) -> RunEnd:
        """End a native run whose final step was not fully executed/adopted."""
        self._require("step")
        return self._terminate("incomplete", reason, details)

    def _close(self, primary: BaseException | None = None) -> None:
        self._control_ready()
        if self._phase == "closed":
            return
        raised = None
        try:
            if self._phase != "stopped":
                if primary is not None:
                    try:
                        self._failure("execution", "caller", primary)
                    finally:
                        self._stop_after_error(primary)
                elif self._phase == "ready":
                    self.pause("Recorder closed at a committed boundary")
                else:
                    self.cancel("Recorder closed without a complete boundary")
        except BaseException as error:
            raised = error
        finally:
            self._phase = "closed"
            if self._cleanup is not None:
                try:
                    _resolved(self._cleanup())
                except BaseException as error:
                    try:
                        self._failure("cleanup", "cleanup", error)
                    except BaseException as diagnostic:
                        error.add_note(f"cleanup diagnostic could not be persisted: {diagnostic!r}")
                    if primary is not None or raised is not None:
                        (primary if primary is not None else raised).add_note(
                            f"resource cleanup also failed: {error!r}"
                        )
                    else:
                        raised = error
        if raised is not None:
            if primary is not None:
                primary.add_note(f"Recorder close also failed: {raised!r}")
            else:
                raise raised

    def close(self) -> None:
        """Stop once and clean only explicitly assigned resources; caller owns the store."""
        self._close()

    def __enter__(self) -> Recorder:
        try:
            self.start()
        except BaseException as error:
            self._close(error)
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._close(exc)
