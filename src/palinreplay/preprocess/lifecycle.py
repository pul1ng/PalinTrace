"""Deterministic lifecycle reduction over verified service evidence."""

from dataclasses import dataclass
from typing import Literal

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
    probe_event: VerifiedEvent
    stop_event: VerifiedEvent | None
    last_live_event: VerifiedEvent
    open_end: bool

    def document(self) -> dict:
        return {
            "name": self.name,
            "start_inclusive": self.start_inclusive,
            "end_exclusive": self.end_exclusive,
            "open_end": self.open_end,
            "start": _event_document(self.start_event),
            "probe": _event_document(self.probe_event),
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
        "request_quote": event.request_span.quote if event.request_span else None,
        "output_quote": event.output_span.quote if event.output_span else None,
        "executable": event.executable,
    }


def _ordered_events(service: ServiceEvidence) -> tuple[VerifiedEvent, ...]:
    indexed = []
    for item in service.items:
        for position, event in enumerate(item.events):
            indexed.append((event.node_index, event.tool_ordinal, position, event))
    indexed.sort(key=lambda row: row[:3])
    return tuple(row[3] for row in indexed)


def _prefer_start_event(
    event: VerifiedEvent, pending_start: VerifiedEvent | None, first_up: VerifiedEvent | None
) -> VerifiedEvent:
    if event.action == "start":
        return event
    if pending_start is not None:
        return pending_start
    if first_up is not None:
        return first_up
    return event


def _lifecycle(
    service: ServiceEvidence, start_index: int, end_index: int
) -> ServiceLifecycle:
    events = _ordered_events(service)
    intervals: list[ServiceInterval] = []
    unknowns: list[StateUnknown] = []
    state: Literal["down", "up", "unknown"] = "down"
    unknown_start: int | None = None
    pending_start: VerifiedEvent | None = None
    current: dict | None = None

    def open_unknown(node: int) -> None:
        nonlocal unknown_start
        if unknown_start is None:
            unknown_start = node

    def close_unknown(node: int, reason: str) -> None:
        nonlocal unknown_start
        if unknown_start is not None and node > unknown_start:
            unknowns.append(
                StateUnknown(
                    name=service.name,
                    start_inclusive=unknown_start,
                    end_exclusive=node,
                    reason=reason,
                )
            )
        unknown_start = None

    def begin(node: int, event: VerifiedEvent) -> None:
        nonlocal current
        chosen = _prefer_start_event(
            event,
            pending_start,
            current["first_up"] if current else None,
        )
        current = {
            "start": chosen,
            "first_up": event,
            "probe": event if event.action == "probe" else None,
            "last_live": event,
        }

    def extend(event: VerifiedEvent) -> None:
        nonlocal current
        if current is None:
            return
        current["last_live"] = event
        if current["probe"] is None and event.action == "probe":
            current["probe"] = event

    def finish(node: int, stop: VerifiedEvent | None, *, open_end: bool) -> None:
        nonlocal current
        if current is None:
            return
        end_exclusive = end_index + 1 if open_end else node
        if end_exclusive > current["first_up"].node_index:
            intervals.append(
                ServiceInterval(
                    name=service.name,
                    start_inclusive=current["first_up"].node_index,
                    end_exclusive=end_exclusive,
                    start_event=current["start"],
                    probe_event=current["probe"] or current["first_up"],
                    stop_event=stop,
                    last_live_event=current["last_live"],
                    open_end=open_end,
                )
            )
        current = None

    for event in events:
        node = event.node_index
        if event.observed_state == "up":
            if state != "up":
                if state == "unknown":
                    close_unknown(node, "service state was unknown before the next live observation")
                current = None
                begin(node, event)
                state = "up"
            elif event.action == "start":
                # A successful start while already live may replace the instance.
                finish(node, None, open_end=False)
                begin(node, event)
            else:
                extend(event)
            pending_start = None
            continue

        if event.observed_state == "down":
            if state == "up":
                finish(node, event, open_end=False)
            if state == "unknown":
                close_unknown(node, "service state was unknown before a down observation")
            state = "down"
            pending_start = None
            continue

        # Unknown state after an action. It interrupts a confirmed live interval;
        # a start attempt is retained as a possible launch for delayed readiness.
        if event.action == "start":
            pending_start = event
        if state == "up":
            finish(node, None, open_end=False)
        open_unknown(node)
        state = "unknown"

    if state == "up":
        assert current is not None
        last_live = current["last_live"].node_index
        if last_live >= end_index:
            finish(end_index, None, open_end=True)
        else:
            finish(last_live + 1, None, open_end=False)
            unknowns.append(
                StateUnknown(
                    name=service.name,
                    start_inclusive=last_live + 1,
                    end_exclusive=end_index + 1,
                    reason="no later state evidence confirms the service remained live",
                )
            )
    elif state == "unknown":
        close_unknown(end_index + 1, "service state remained unknown through the analysis end")

    # A first observation at start_index does not need an earlier state guess:
    # the source trajectory has no earlier service start by construction.
    del start_index
    return ServiceLifecycle(service=service, intervals=tuple(intervals), unknowns=tuple(unknowns))


def analyze_lifecycles(
    services: tuple[ServiceEvidence, ...], start_index: int, end_index: int
) -> tuple[ServiceLifecycle, ...]:
    if start_index > end_index:
        raise ValueError("lifecycle start must not follow its end")
    return tuple(_lifecycle(service, start_index, end_index) for service in services)
