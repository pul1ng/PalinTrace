"""Scoped logical IO measurements; never infer physical device traffic or cache hits.

Nested scopes are inclusive. Compare siblings or the outer boundary, never sum a
parent with its children. No paths or payloads are included in exported metrics.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from time import monotonic

from palinreplay.diagnostics.timing import is_tracing, span

COUNTERS = (
    "read_bytes", "written_bytes", "copied_bytes", "hashed_bytes",
    "verified_bytes", "verification_calls", "verification_reused",
    "exported_bytes", "manifest_content_bytes",
)
_active: ContextVar[tuple] = ContextVar("palinreplay_io_metrics", default=())


@dataclass
class IOMetrics:
    counters: dict = field(default_factory=lambda: dict.fromkeys(COUNTERS, 0))
    phases: dict = field(default_factory=dict)
    temporary: dict = field(default_factory=dict)
    temporary_peak_bytes: int = 0

    def snapshot(self):
        return {
            "schema_version": "palinreplay.io-metrics.v1",
            "scope": "instrumented-logical-io-inclusive",
            **self.counters,
            "temporary_peak_bytes": self.temporary_peak_bytes,
            "phases_sec": dict(self.phases),
        }


@contextmanager
def measure():
    metrics = IOMetrics()
    token = _active.set((*_active.get(), metrics))
    try:
        yield metrics
    finally:
        _active.reset(token)


def count(**values):
    for metrics in _active.get():
        for key, value in values.items():
            metrics.counters[key] += value


def temporary_size(path, size):
    for metrics in _active.get():
        metrics.temporary[str(path)] = size
        metrics.temporary_peak_bytes = max(
            metrics.temporary_peak_bytes, sum(metrics.temporary.values())
        )


def forget_temporary(path):
    for metrics in _active.get():
        metrics.temporary.pop(str(path), None)


@contextmanager
def temporary_file(path):
    temporary_size(path, 0)
    try:
        yield
    finally:
        forget_temporary(path)


@contextmanager
def phase(name):
    with span(name):
        if not _active.get():
            yield
            return
        started = monotonic()
        try:
            yield
        finally:
            elapsed = monotonic() - started
            for metrics in _active.get():
                metrics.phases[name] = metrics.phases.get(name, 0) + elapsed


def timed(name):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            if not _active.get() and not is_tracing():
                return function(*args, **kwargs)
            with phase(name):
                return function(*args, **kwargs)
        return call
    return decorate


class CountingReader:
    """Count bytes actually returned by read(), including tar headers and padding."""

    def __init__(self, stream, *, copying=False):
        self.stream = stream
        self.copying = copying

    def read(self, size=-1):
        data = self.stream.read(size)
        count(read_bytes=len(data))
        if self.copying:
            count(copied_bytes=len(data))
        return data

    def __getattr__(self, name):
        return getattr(self.stream, name)


def boundary_summary(rows):
    """Separate initialization from later boundaries; old missing IO stays unknown."""
    def group(items):
        ios = [r["io"] for r in items if isinstance(r.get("io"), dict)]
        return {
            "count": len(items),
            "duration_sec": sum(r["duration_sec"] for r in items),
            "io": {
                **{k: sum(v[k] for v in ios) for k in COUNTERS},
                "temporary_peak_bytes": max(
                    (v["temporary_peak_bytes"] for v in ios), default=0
                ),
            } if len(ios) == len(items) else None,
        }

    return {
        "initial": group([r for r in rows if r["index"] == 0]),
        "steps": group([r for r in rows if r["index"] > 0]),
    }
