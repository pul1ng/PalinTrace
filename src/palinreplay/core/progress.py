"""Read-only recording events; observers cannot change recording decisions."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ProgressEvent:
    # Only public identifiers and counters: never prompts, commands or SDK errors.
    phase: Literal[
        "preparing",
        "connecting",
        "starting",
        "capturing",
        "committing",
        "committed",
        "model",
        "model_returned",
        "retry",
        "tool",
        "tool_returned",
        "cleanup",
        "verifying",
        "finished",
        "flow_phase",
        "flow_start",
        "flow_end",
    ]
    task_id: str | None = None
    step: int | None = None
    request: int | None = None
    attempt: int | None = None
    tool: int | None = None
    tools: int | None = None
    status: str | None = None
    run_end_status: str | None = None
    code: str | None = None
    subcode: str | None = None
    depth: int = 0
    label: str | None = None
    duration_sec: float | None = None
    inclusive_sec: float | None = None


ProgressCallback = Callable[[ProgressEvent], None]


class ProgressReporter:
    """A broken observer must not abort recording or trigger a model retry.

    Disable it after the first ordinary error. Cancellation still propagates.
    """

    def __init__(self, callback: ProgressCallback | None = None):
        self.callback = callback

    def __call__(self, event: ProgressEvent):
        if self.callback is not None:
            try:
                self.callback(event)
            except Exception:
                self.callback = None
