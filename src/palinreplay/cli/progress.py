"""Rich terminal display and plain text rendering of recording events."""

from __future__ import annotations

import time

from palinreplay.cli._progress import ProgressDisplay
from palinreplay.core.progress import ProgressEvent, ProgressReporter


def describe(event: ProgressEvent) -> str:
    if event.phase == "flow_phase":
        action = "开始" if event.status == "started" else f"结束, 耗时 {event.duration_sec:.6f}s"
        return f"阶段 {event.code} {event.label}: {action}"
    if event.phase in ("flow_start", "flow_end"):
        prefix = ""
        if event.step is not None:
            if event.code.startswith("S") or event.code == "1.6":
                prefix = f"C{event.step} · "
            elif event.code.startswith("2."):
                prefix = f"Step {event.step} · "
        heading = f"{'  ' * event.depth}{prefix}{event.subcode or event.code} {event.label}"
        if event.tool is not None:
            heading += f" · 工具 {event.tool}/{event.tools}"
        if event.phase == "flow_start":
            return f"{heading}: 开始"
        status = "结束 (控制流/异常)" if event.status == "raised" else "结束"
        result = f"{heading}: {status}, 本段耗时 {event.duration_sec:.6f}s"
        if event.inclusive_sec - event.duration_sec > 0.000001:
            result += f", 含子项 {event.inclusive_sec:.6f}s"
        return result
    if event.phase == "capturing":
        return f"Capturing C{event.step}: saving file state"
    if event.phase == "committing":
        return f"Committing C{event.step}: context and file state"
    if event.phase == "committed":
        return f"Committed C{event.step}"
    if event.phase == "model":
        return f"Step {event.step}: model request {event.request} (attempt {event.attempt})"
    if event.phase == "model_returned":
        return f"Step {event.step}: model response received"
    if event.phase == "retry":
        return f"Step {event.step}: waiting to retry model (attempt {event.attempt})"
    if event.phase in ("tool", "tool_returned"):
        action = "running" if event.phase == "tool" else "returned"
        return f"Step {event.step}: tool {event.tool}/{event.tools} {action}"
    if event.phase == "finished":
        if event.status == "recorded" and event.run_end_status == "paused":
            return "Record saved: paused at Agent budget"
        return {
            "recorded": "Record saved and verified",
            "incomplete": "Record incomplete: unfinished step retained",
            "cancelled": "Record cancelled",
            "failed": "Record failed: see report for details",
        }.get(event.status, "Record stopped")
    return {
        "preparing": "Preparing task and model client",
        "connecting": "Connecting to Docker",
        "starting": "Starting task container",
        "cleanup": "Cleaning up run resources",
        "verifying": "Verifying saved trajectory",
    }[event.phase]


class RecordProgressDisplay(ProgressDisplay):
    """Mini-style Live/spinner view on stderr; plain events when redirected.

    The bar measures committed steps against a budget, not task completion.
    Rich is optional and is imported only when starting an interactive view.
    """

    def __init__(self, max_steps: int, mode="auto", *, stream=None, console=None):
        super().__init__(mode, stream=stream, console=console)
        self.max_steps = max_steps
        self.committed_steps = 0
        self.last_node = "none"
        self.task_id = ""
        self._live = None
        self._notify = ProgressReporter(self._update)

    def _start_live(self):
        from rich.console import Group
        from rich.live import Live
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )
        from rich.text import Text

        console = self._terminal_console()
        if console is None:
            return
        self._title = Text("Record", style="bold cyan")
        self._activity = Progress(
            SpinnerColumn("dots2"),
            TextColumn("{task.description}", markup=False),
            TimeElapsedColumn(),
            console=console,
            auto_refresh=False,
        )
        self._activity_id = self._activity.add_task("Preparing recording", total=None)
        self._steps = Progress(
            TextColumn("{task.description}", markup=False),
            BarColumn(bar_width=24),
            MofNCompleteColumn(),
            TextColumn("Last node: {task.fields[node]}", markup=False),
            console=console,
            auto_refresh=False,
        )
        self._steps_id = self._steps.add_task(
            "Committed steps (budget)", total=self.max_steps, node=self.last_node
        )
        self._live = Live(
            Group(self._title, self._activity, self._steps),
            console=console,
            refresh_per_second=4,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self._live.start(refresh=True)

    def __call__(self, event: ProgressEvent):
        self._notify(event)
        if self._notify.callback is None:
            self._stop_live()

    def _update(self, event: ProgressEvent):
        if self.mode == "off":
            return
        if event.task_id is not None:
            self.task_id = event.task_id
        if event.phase == "committed":
            self.committed_steps = event.step
            self.last_node = f"C{event.step}"
        message = describe(event)
        if event.phase == "finished":
            self.finished = True
        if self._live is not None:
            self._title.plain = f"Record {self.task_id}".rstrip()
            self._activity.update(self._activity_id, description=message)
            self._steps.update(self._steps_id, completed=self.committed_steps, node=self.last_node)
            if event.phase in ("flow_phase", "flow_start", "flow_end"):
                # Keep fast substeps in terminal history, not just in a spinner
                # that the next callback can immediately overwrite.
                elapsed = time.monotonic() - self._started
                self._live.console.print(
                    f"[record {elapsed:.1f}s] {message}", markup=False, highlight=False,
                )
            if self.finished:
                self._activity.update(self._activity_id, total=1, completed=1)
                self._activity.stop_task(self._activity_id)
                self._steps.stop_task(self._steps_id)
                self._live.refresh()
        else:
            elapsed = time.monotonic() - self._started
            print(f"[record {elapsed:.1f}s] {message}", file=self.stream, flush=True)

    def _stop_live(self):
        live, self._live = self._live, None
        if live is not None:
            try:
                live.stop()
            except Exception:
                pass

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is not None and not self.finished:
                self(
                    ProgressEvent(
                        "finished",
                        status="cancelled" if isinstance(exc, KeyboardInterrupt) else "failed",
                    )
                )
        finally:
            self._stop_live()
