"""Optional monotonic timing tree. Names/counters only, never call arguments or errors.

Durations include children; self time subtracts direct child durations. The trace
is bounded, but aggregates continue counting if the detailed timeline fills up.
This measures instrumented application stages, not a CPU or system-call profile.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic

_active: ContextVar[tuple | None] = ContextVar("palinreplay_debug_timing", default=None)


@dataclass
class DebugTiming:
    started: float = field(default_factory=monotonic)
    max_spans: int = 50_000
    spans: list = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    count: int = 0

    def snapshot(self):
        return {
            "schema_version": "palinreplay.debug-timing.v1",
            "clock": "time.monotonic",
            "scope": "instrumented-application-stages",
            "timeline_complete": self.count == len(self.spans),
            "dropped_spans": self.count - len(self.spans),
            "totals": {name: dict(values) for name, values in self.totals.items()},
            "spans": [
                {key: value for key, value in row.items() if key != "_children_sec"}
                for row in self.spans
            ],
        }


def is_tracing():
    return _active.get() is not None


@contextmanager
def debug_timing(enabled, *, max_spans=50_000):
    timing = DebugTiming(max_spans=max_spans) if enabled else None
    token = _active.set((timing, None) if timing else None)
    try:
        yield timing
    finally:
        _active.reset(token)


@contextmanager
def span(name, *, step=None, request=None, attempt=None, tool=None):
    active = _active.get()
    if active is None:
        yield
        return
    timing, parent = active
    started = monotonic()
    row = {
        "id": timing.count,
        "parent_id": parent["id"] if parent is not None else None,
        "name": name,
        "offset_sec": started - timing.started,
        "status": "ok",
        "_children_sec": 0.0,
        **{key: value for key, value in (
            ("step", step), ("request", request), ("attempt", attempt), ("tool", tool)
        ) if value is not None},
    }
    timing.count += 1
    if len(timing.spans) < timing.max_spans:
        timing.spans.append(row)
    token = _active.set((timing, row))
    try:
        yield
    except BaseException as error:
        # Includes normal Agent control exceptions (e.g. Submitted). Run status
        # remains authoritative; an exception message is deliberately not saved.
        row.update(status="raised", exception_type=type(error).__name__)
        raise
    finally:
        elapsed = monotonic() - started
        row["duration_sec"] = elapsed
        row["self_sec"] = max(0.0, elapsed - row["_children_sec"])
        total = timing.totals.setdefault(
            name, {"count": 0, "duration_sec": 0.0, "self_sec": 0.0, "raised": 0}
        )
        total["count"] += 1
        total["duration_sec"] += elapsed
        total["self_sec"] += row["self_sec"]
        total["raised"] += row["status"] == "raised"
        if parent is not None:
            parent["_children_sec"] += elapsed
        _active.reset(token)
