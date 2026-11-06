"""Record synchronous backend calls without replacing the Agent's control flow.

Preparing, invoking, handing off and acknowledging adoption are explicit phases.
There is no implicit retry, acceptance, step completion or Node publication.
"""

from __future__ import annotations

import inspect
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Generic, TypeVar

from palinreplay.core.interfaces import CallAdapter, Payload, RequestT, ResultCapture, ResultT
from palinreplay.core.models import (
    ArtifactRef,
    CallEvidence,
    Event,
    Execution,
    Failure,
    Operation,
    Step,
)
from palinreplay.diagnostics.metrics import timed
from palinreplay.diagnostics.timing import span
from palinreplay.storage.payloads import ROOT_MEDIA, logical_identity
from palinreplay.storage.store import ConflictError, IncompleteRecordError, RecordStore, StoreError

ValueT = TypeVar("ValueT")
ReceiptT = TypeVar("ReceiptT")


class RecordingError(StoreError):
    """Recording failed; the backend or receiver may already have acted."""

    def __init__(
        self,
        phase: str,
        error: BaseException,
        *,
        execution_id: str | None,
        backend_error: BaseException | None = None,
    ):
        super().__init__(f"recording failed at {phase} (execution={execution_id})")
        self.phase = phase
        self.execution_id = execution_id
        self.recording_error = error
        self.backend_error = backend_error
        self.diagnostic_error: BaseException | None = None


class RecordingStoppedError(StoreError):
    """This port instance stopped after a recording failure; do not repeat side effects."""


@dataclass(frozen=True)
class CallTrace:
    operation: Operation
    execution: Execution
    events: tuple[Event, ...]
    failures: tuple[Failure, ...]
    evidence: tuple[CallEvidence, ...] = ()

    @property
    def phase(self) -> str:
        if any(event.kind == "result" and event.outcome == "unknown" for event in self.events):
            return "unknown"
        return self.events[-1].kind if self.events else "prepared"


def read_call(store: RecordStore, execution_id: str) -> CallTrace:
    """Read persisted evidence only; never invoke a backend or a receiver."""
    execution = store.get(Execution, execution_id)
    operation = store.get(Operation, execution.operation_id)
    events = store.execution_events(execution_id)
    failures = tuple(
        failure
        for failure in store.list(Failure, run_id=execution.run_id)
        if failure.execution_id == execution_id
    )
    for failure in failures:
        store.verify_artifact(failure.details)
    evidence = tuple(e for e in store.list(CallEvidence, run_id=execution.run_id)
                     if e.execution_id == execution_id)
    for item in evidence:
        store.verify_artifact(item.payload)
    return CallTrace(operation, execution, events, failures, evidence)


def _synchronous(function: Callable, label: str) -> None:
    targets = (function, getattr(function, "__call__", None))
    if not callable(function) or any(
        inspect.iscoroutinefunction(target)
        or inspect.isasyncgenfunction(target)
        or inspect.isgeneratorfunction(target)
        for target in targets
    ):
        raise TypeError(f"{label} must be a synchronous callable")


def _resolved(value: Any) -> Any:
    # Do not silently execute an event loop or consume an unknown output stream.
    if inspect.isawaitable(value) or inspect.isgenerator(value) or inspect.isasyncgen(value):
        if inspect.iscoroutine(value) or inspect.isgenerator(value):
            value.close()
        raise TypeError("async or streaming values require a dedicated adapter; none was consumed")
    return value


def _payload(value: Any) -> Payload:
    value = _resolved(value)
    if not isinstance(value, Payload):
        raise TypeError("capture function must return Payload, never repr/str fallback")
    return value


def _capture(value: Any) -> ResultCapture:
    value = _resolved(value)
    if not isinstance(value, ResultCapture):
        raise TypeError("result capture function must return ResultCapture")
    return value


def _error_details(error: BaseException) -> dict:
    return {"type": f"{type(error).__module__}.{type(error).__qualname__}", "message": str(error)}


class RecordingPorts:
    """One synchronous, thread-confined I/O boundary for an already-created Step.

    Each prepared call has its own durable identity. For parallel upstream APIs use
    an explicit future parallel adapter, not a lock that serializes their behavior.
    """

    def __init__(self, store: RecordStore, step_id: str):
        self.store = store
        self.step = store.get(Step, step_id)
        self._thread = threading.get_ident()
        self._busy = False
        self._failure: RecordingError | None = None

    def _ready(self) -> None:
        if threading.get_ident() != self._thread:
            raise RuntimeError("RecordingPorts is synchronous and thread-confined")
        if self._failure is not None:
            raise RecordingStoppedError(
                "recording stopped; inspect persisted facts before recovery"
            ) from self._failure
        if self._busy:
            raise RuntimeError("reentrant backend/receiver calls are unsupported")

    def _put(self, payload: Payload) -> ArtifactRef:
        payload = _payload(payload)
        return self.store.put_payload(payload)

    def _failure_record(
        self, category: str, phase: str, details: dict, execution_id: str | None
    ) -> None:
        self.store.save(
            Failure(
                run_id=self.step.run_id,
                attempt_id=self.step.attempt_id,
                step_id=self.step.step_id,
                execution_id=execution_id,
                category=category,
                details=self.store.put_payload_json({"phase": phase, **details}),
            )
        )

    def _abort(
        self,
        phase: str,
        error: BaseException,
        execution_id: str | None = None,
        backend_error: BaseException | None = None,
    ) -> None:
        failure = RecordingError(
            phase, error, execution_id=execution_id, backend_error=backend_error
        )
        self._failure = failure
        try:
            self._failure_record(
                "recording",
                phase,
                {
                    "recording_error": _error_details(error),
                    "backend_error": _error_details(backend_error)
                    if backend_error is not None
                    else None,
                    "handoff_may_have_occurred": phase in ("handoff", "delivery", "acceptance"),
                },
                execution_id,
            )
        except BaseException as diagnostic_error:
            failure.diagnostic_error = diagnostic_error
        # Cancellation remains cancellation even when its diagnostic cannot be saved.
        control = (
            backend_error
            if backend_error is not None and not isinstance(backend_error, Exception)
            else error
        )
        if not isinstance(control, Exception):
            control.add_note(str(failure))
            raise control from failure
        raise failure from error

    def model(
        self, adapter: CallAdapter[RequestT, ResultT], request: RequestT
    ) -> RecordedCall[RequestT, ResultT]:
        return self._prepare("model", adapter, request)

    def tool(
        self,
        adapter: CallAdapter[RequestT, ResultT],
        request: RequestT,
        *,
        tool_call_id: str,
        caused_by_execution_id: str | None = None,
    ) -> RecordedCall[RequestT, ResultT]:
        return self._prepare(
            "tool",
            adapter,
            request,
            tool_call_id=tool_call_id,
            caused_by_execution_id=caused_by_execution_id,
        )

    @timed("call.prepare")
    def _prepare(
        self,
        kind: str,
        adapter: CallAdapter,
        request: Any,
        *,
        tool_call_id: str | None = None,
        caused_by_execution_id: str | None = None,
        operation: Operation | None = None,
    ) -> RecordedCall:
        self._ready()
        for label in (
            "execute",
            "capture_request",
            "capture_result",
            "capture_context",
            "capture_exception",
        ):
            function = getattr(adapter, label)
            if function is not None:
                _synchronous(function, label)
        self._busy = True
        try:
            operation = operation or self.store.save(
                Operation(
                    run_id=self.step.run_id,
                    step_id=self.step.step_id,
                    kind=kind,
                    name=adapter.name,
                    tool_call_id=tool_call_id,
                    caused_by_execution_id=caused_by_execution_id,
                )
            )
            request_ref = self._put(adapter.capture_request(request))
            context_ref = (
                self._put(adapter.capture_context(request)) if adapter.capture_context else None
            )
            previous = [
                execution
                for execution in self.store.list(Execution, run_id=self.step.run_id)
                if execution.operation_id == operation.operation_id
            ]
            execution = self.store.save(
                Execution(
                    run_id=self.step.run_id,
                    operation_id=operation.operation_id,
                    index=len(previous) + 1,
                    request=request_ref,
                    context=context_ref,
                    visibility=adapter.visibility,
                )
            )
            return RecordedCall(self, adapter, request, operation, execution)
        except BaseException as error:
            self._abort("prepare", error)
        finally:
            self._busy = False

    def retry(self, previous: RecordedCall[RequestT, ResultT]) -> RecordedCall[RequestT, ResultT]:
        """Explicit caller-owned retry of a known error; never retry an unknown outcome."""
        self._ready()
        if previous._ports is not self:
            raise ValueError("retry must refer to a call prepared by these ports")
        trace = read_call(self.store, previous.execution.execution_id)
        result = next((event for event in trace.events if event.kind == "result"), None)
        if (
            result is None
            or result.outcome != "error"
            or any(e.kind == "accepted" for e in trace.events)
        ):
            raise IncompleteRecordError("retry requires a known error that has not been accepted")
        attempts = [
            e
            for e in self.store.list(Execution, run_id=self.step.run_id)
            if e.operation_id == previous.operation.operation_id
        ]
        if previous.execution.index != max(e.index for e in attempts):
            raise ConflictError("retry must refer to the latest execution of the operation")
        previous._unchanged(
            previous._adapter.capture_request(previous._request), previous.execution.request
        )
        if previous._adapter.capture_context:
            previous._unchanged(
                previous._adapter.capture_context(previous._request), previous.execution.context
            )
        return self._prepare(
            previous.operation.kind,
            previous._adapter,
            previous._request,
            operation=previous.operation,
        )


@timed("call.deliver_batch")
def deliver_batch(
    deliveries: list[tuple[RecordedCall, ValueT]],
    *,
    encode: Callable[[ValueT], Payload],
    receive: Callable[[list[ValueT]], ReceiptT],
) -> ReceiptT:
    """Persist every offered view before one native batch handoff.

    Adoption is still explicit and must be read from actual Agent state. A
    partially persisted handoff is fatal; the receiver must never be repeated.
    """
    if not deliveries:
        raise ValueError("batch must contain at least one call")
    ports = deliveries[0][0]._ports
    ports._ready()
    _synchronous(encode, "observation encoder")
    _synchronous(receive, "receiver")
    if len({id(call) for call, _ in deliveries}) != len(deliveries):
        raise ValueError("duplicate call in delivery batch")
    for call, _ in deliveries:
        if call._ports is not ports:
            raise ValueError("batch calls must belong to the same ports")
        if call._delivery_attempted:
            raise ConflictError("delivery has already been attempted")
        try:
            events = ports.store.execution_events(call.execution_id)
        except BaseException as error:
            ports._abort("delivery-check", error, call.execution_id)
        if not call._invoked or not events or events[-1].kind != "result":
            raise IncompleteRecordError("delivery requires a persisted result for every call")
    for call, _ in deliveries:
        call._delivery_attempted = True
    ports._busy = True
    try:
        try:
            offered = [ports._put(encode(value)) for _, value in deliveries]
        except BaseException as error:
            ports._abort("observation", error)
        try:
            receipt = _resolved(receive([value for _, value in deliveries]))
        except BaseException as error:
            ports._abort("handoff", error)
        try:
            for (call, _), payload in zip(deliveries, offered, strict=True):
                ports.store.append_event(call.execution_id, "delivered", payload=payload)
        except BaseException as error:
            ports._abort("delivery", error, call.execution_id)
        return receipt
    finally:
        ports._busy = False


class RecordedCall(Generic[RequestT, ResultT]):
    def __init__(
        self,
        ports: RecordingPorts,
        adapter: CallAdapter[RequestT, ResultT],
        request: RequestT,
        operation: Operation,
        execution: Execution,
    ):
        self._ports, self._adapter, self._request = ports, adapter, request
        self._operation, self._execution = operation, execution
        self._invoked = False
        self._delivery_attempted = False
        self._acceptance_attempted = False

    @property
    def operation(self) -> Operation:
        return self._operation

    @property
    def execution(self) -> Execution:
        return self._execution

    @property
    def execution_id(self) -> str:
        return self.execution.execution_id

    @timed("call.observe")
    def observe(
        self, kind: str, value: ValueT, *, encode: Callable[[ValueT], Payload] = _payload
    ) -> CallEvidence:
        """Persist internal evidence during backend entry; never repeat a failed write."""
        ports = self._ports
        if threading.get_ident() != ports._thread or not ports._busy or not self._invoked:
            raise RuntimeError("internal evidence can only be observed during this call")
        if ports._failure:
            raise ports._failure
        try:
            _synchronous(encode, "internal evidence encoder")
            previous = [e for e in ports.store.list(CallEvidence, run_id=ports.step.run_id)
                        if e.execution_id == self.execution_id]
            return ports.store.save(CallEvidence(
                run_id=ports.step.run_id, execution_id=self.execution_id,
                index=len(previous) + 1, kind=kind, payload=ports._put(encode(value)),
            ))
        except BaseException as error:
            ports._abort("internal-evidence", error, self.execution_id)

    def _unchanged(self, payload: Payload, ref: ArtifactRef) -> None:
        payload = _payload(payload)
        identity = ((ref.sha256, ref.size, ref.media_type) if ref.media_type != ROOT_MEDIA
                    else logical_identity(self._ports.store.read_bytes(ref), ref.media_type))
        if (sha256(payload.data).hexdigest(), len(payload.data), payload.media_type) != identity:
            raise ValueError("request/context changed after preparation; backend was not invoked")

    @timed("call.save_result")
    def _save_result(
        self,
        capture: ResultCapture,
        started: datetime,
        finished: datetime,
        elapsed: float,
        backend_error: BaseException | None = None,
    ) -> None:
        details = {
            "capture": {"source": capture.source, "limitations": capture.limitations},
            "backend": capture.details,
            "timing": {
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "duration_sec": elapsed,
            },
            "exception": _error_details(backend_error) if backend_error is not None else None,
        }
        raw_ref = self._ports._put(capture.raw)
        details_ref = self._ports.store.put_payload_json(details)
        self._ports.store.append_event(
            self.execution_id,
            "result",
            payload=raw_ref,
            details=details_ref,
            outcome=capture.outcome,
        )

    def invoke(self) -> ResultT:
        """Enter the backend at most once; return its original value only after persistence."""
        ports = self._ports
        ports._ready()
        if self._invoked:
            raise ConflictError("a call handle can be invoked only once; no backend was repeated")
        self._invoked = True
        ports._busy = True
        try:
            try:
                self._unchanged(
                    self._adapter.capture_request(self._request), self.execution.request
                )
                if self._adapter.capture_context:
                    self._unchanged(
                        self._adapter.capture_context(self._request), self.execution.context
                    )
                attempts = [
                    e
                    for e in ports.store.list(Execution, run_id=ports.step.run_id)
                    if e.operation_id == self.operation.operation_id
                    and e.execution_id != self.execution_id
                ]
                if any(
                    e.kind == "accepted"
                    for attempt in attempts
                    for e in ports.store.execution_events(attempt.execution_id)
                ):
                    raise ConflictError("logical operation already has an accepted execution")
                ports.store.append_event(
                    self.execution_id,
                    "dispatched",
                    details=ports.store.put_payload_json(
                        {
                            "boundary": "before_backend_entry",
                            "visibility": self.execution.visibility,
                        }
                    ),
                )
            except BaseException as error:
                ports._abort("dispatch", error, self.execution_id)
            started, start_tick = datetime.now(UTC), time.monotonic()
            try:
                with span(f"{self.operation.kind}.execute", step=ports.step.index):
                    value = self._adapter.execute(self._request)
            except BaseException as backend_error:
                if ports._failure is not None:
                    # An observer already persisted/attempted the recording failure.
                    # Do not disguise it as a backend error or try to write after RunEnd.
                    raise
                finished, elapsed = datetime.now(UTC), time.monotonic() - start_tick
                try:
                    if self._adapter.capture_exception:
                        captured = _capture(self._adapter.capture_exception(backend_error))
                        if isinstance(backend_error, TimeoutError) or not isinstance(
                            backend_error, Exception
                        ):
                            # Preserve any exposed partial bytes, without making a
                            # timeout/cancellation into a known completed result.
                            captured = replace(captured, outcome="unknown")
                        elif captured.outcome == "returned":
                            raise ValueError(
                                "an exception cannot be classified as a returned result"
                            )
                    else:
                        captured = ResultCapture(
                            raw=Payload.json(_error_details(backend_error)),
                            source="backend-exception",
                            outcome="unknown",
                            limitations=("No complete backend response was exposed.",),
                        )
                    self._save_result(captured, started, finished, elapsed, backend_error)
                    ports._failure_record(
                        "execution",
                        "backend",
                        {
                            "error": _error_details(backend_error),
                            "outcome": captured.outcome,
                        },
                        self.execution_id,
                    )
                except BaseException as recording_error:
                    ports._abort("result", recording_error, self.execution_id, backend_error)
                raise
            finished, elapsed = datetime.now(UTC), time.monotonic() - start_tick
            try:
                _resolved(value)
                captured = _capture(self._adapter.capture_result(value))
                self._save_result(captured, started, finished, elapsed)
            except BaseException as recording_error:
                ports._abort("result", recording_error, self.execution_id)
            return value
        finally:
            ports._busy = False

    def deliver(
        self,
        observation: ValueT,
        *,
        encode: Callable[[ValueT], Payload],
        receive: Callable[[ValueT], ReceiptT],
    ) -> ReceiptT:
        """Persist the offered view, call the receiver once, then record its handoff acknowledgement.

        A normal receiver return acknowledges delivery, not adoption. A receiver
        exception leaves handoff uncertain; it is never retried automatically.
        """
        ports = self._ports
        ports._ready()
        _synchronous(encode, "observation encoder")
        _synchronous(receive, "receiver")
        if self._delivery_attempted:
            raise ConflictError("delivery has already been attempted")
        try:
            events = ports.store.execution_events(self.execution_id)
        except BaseException as error:
            ports._abort("delivery-check", error, self.execution_id)
        if not self._invoked or not events or events[-1].kind != "result":
            raise IncompleteRecordError("delivery requires a persisted result for this call")
        self._delivery_attempted = True
        ports._busy = True
        try:
            try:
                offered = ports._put(encode(observation))
            except BaseException as error:
                ports._abort("observation", error, self.execution_id)
            try:
                receipt = _resolved(receive(observation))
            except BaseException as error:
                ports._abort("handoff", error, self.execution_id)
            try:
                ports.store.append_event(self.execution_id, "delivered", payload=offered)
            except BaseException as error:
                ports._abort("delivery", error, self.execution_id)
            return receipt
        finally:
            ports._busy = False

    @timed("call.accept")
    def accept(self, adopted: ValueT, *, encode: Callable[[ValueT], Payload]) -> Event:
        """Record an adapter-observed Agent adoption snapshot, never infer it from a return."""
        ports = self._ports
        ports._ready()
        _synchronous(encode, "adoption encoder")
        if self._acceptance_attempted:
            raise ConflictError("acceptance has already been attempted")
        try:
            events = ports.store.execution_events(self.execution_id)
        except BaseException as error:
            ports._abort("acceptance-check", error, self.execution_id)
        if not events or events[-1].kind != "delivered":
            raise IncompleteRecordError("acceptance requires acknowledged delivery")
        self._acceptance_attempted = True
        ports._busy = True
        try:
            try:
                return ports.store.append_event(
                    self.execution_id, "accepted", payload=ports._put(encode(adopted))
                )
            except BaseException as error:
                ports._abort("acceptance", error, self.execution_id)
        finally:
            ports._busy = False
