"""Deterministic lifecycle reduction over verified service evidence."""

from dataclasses import dataclass

from pydantic import Field, StrictInt, StrictStr, field_validator

from palinreplay.core.schema import Spec
from palinreplay.preprocess.evidence import ServiceEvidence, VerifiedEvent


class StateUnknown(Spec):
    name: StrictStr
    start_inclusive: StrictInt = Field(ge=0)
    end_exclusive: StrictInt = Field(ge=0)
    reason: StrictStr

    @field_validator("reason")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("unknown reason must be nonempty")
        return value


@dataclass(frozen=True)
class ServiceInterval:
    name: str
    start_inclusive: int
    end_exclusive: int
    start_event: VerifiedEvent
    probe_event: VerifiedEvent | None
    stop_event: VerifiedEvent | None
    last_live_event: VerifiedEvent
    open_end: bool
    events: tuple[VerifiedEvent, ...] = ()

    def document(self) -> dict:
        return {
            "name": self.name,
            "start_inclusive": self.start_inclusive,
            "end_exclusive": self.end_exclusive,
            "open_end": self.open_end,
            "start": _event_document(self.start_event),
            "probe": _event_document(self.probe_event) if self.probe_event else None,
            "stop": _event_document(self.stop_event) if self.stop_event else None,
            "last_live": _event_document(self.last_live_event),
        }


@dataclass(frozen=True)
class ServiceLifecycle:
    service: ServiceEvidence
    intervals: tuple[ServiceInterval, ...]
    unknowns: tuple[StateUnknown, ...]

    def document(self) -> dict:
        return {
            "service": self.service.document(),
            "intervals": [interval.document() for interval in self.intervals],
            "unknowns": [unknown.model_dump(mode="json") for unknown in self.unknowns],
        }


def _event_document(event: VerifiedEvent) -> dict:
    return {
        "node_index": event.node_index,
        "tool_ordinal": event.tool_ordinal,
        "execution_id": event.execution_id,
        "action": event.action,
        "observed_state": event.observed_state,
        "action_result": event.action_result,
        "result_basis": event.result_basis,
        "call_exit_code": event.call_exit_code,
        "exit_code": event.exit_code,
        "request_quote": event.request_span.quote if event.request_span else None,
        "output_quote": event.output_span.quote if event.output_span else None,
        "executable": event.executable,
    }


def _ordered_events(service: ServiceEvidence) -> tuple[VerifiedEvent, ...]:
    indexed = []
    for item in service.items:
        for position, event in enumerate(item.events):
            offset = event.request_span.start if event.request_span else float("inf")
            indexed.append((event.node_index, event.tool_ordinal, offset, position, event))
    indexed.sort(key=lambda row: row[:4])
    return tuple(row[4] for row in indexed)


def _lifecycle(
    service: ServiceEvidence, start_index: int, end_index: int
) -> ServiceLifecycle:
    intervals = []
    current: list[VerifiedEvent] | None = None

    def finish(end: int, stop: VerifiedEvent | None):
        if current is None or current[0].node_index >= end:
            return
        start = current[0]
        probes = [event for event in current if event.action == "probe"]
        intervals.append(ServiceInterval(
            name=service.name, start_inclusive=start.node_index, end_exclusive=end,
            start_event=start, probe_event=probes[0] if probes else None,
            stop_event=stop, last_live_event=current[-1], open_end=stop is None,
            events=tuple(current),
        ))

    for event in _ordered_events(service):
        if not start_index <= event.node_index <= end_index:
            raise ValueError("service event lies outside the analyzed node range")
        if event.action == "start" and event.request_span is not None and current is None:
            current = [event]
        elif event.action == "stop" and event.action_result == "succeeded":
            if current is None:
                command = event.request_span.quote if event.request_span else None
                raise ValueError(
                    f"{service.name} ({', '.join(service.identity_keys)}) "
                    f"C{event.node_index}/{event.execution_id}: successful stop {command!r} "
                    "has no matching recorded start; missing original startup command"
                )
            finish(event.node_index, event)
            current = None
        elif current is not None:
            current.append(event)
    finish(end_index + 1, None)
    return ServiceLifecycle(service=service, intervals=tuple(intervals), unknowns=())


def analyze_lifecycles(
    services: tuple[ServiceEvidence, ...], start_index: int, end_index: int
) -> tuple[ServiceLifecycle, ...]:
    """Fold launch intent (initially absent), never availability, into half-open intervals."""
    if start_index > end_index:
        raise ValueError("lifecycle start must not follow its end")
    return tuple(_lifecycle(service, start_index, end_index) for service in services)
