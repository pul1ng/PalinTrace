"""Terminal progress for Replay; only completed historical steps advance the bar."""

import sys
import time

from palinreplay.core.progress import ProgressEvent, ProgressReporter


def describe(event: ProgressEvent) -> str:
    position = f"源 C{event.source_step} → C{event.step}"
    if event.phase == "restoring":
        return f"恢复 {position}"
    if event.phase == "replaying":
        return f"执行与保存 {position}"
    if event.phase == "verifying":
        return f"校验 {position}"
    if event.phase == "committed":
        return f"已完成 {position}"
    if event.phase == "finished":
        return event.label or event.status.upper()
    return {
        "preparing": "读取历史轨迹",
        "connecting": "连接 Docker",
        "cleanup": "清理临时资源",
    }[event.phase]


class ReplayProgressDisplay:
    """Live progress on stderr, plain stage updates when redirected."""

    def __init__(self, mode="auto", *, stream=None, console=None):
        self.mode = mode
        self.stream = stream if stream is not None else sys.stderr
        self.console = console
        self.completed = 0
        self.total = None
        self.finished = False
        self._progress = None
        self._notify = ProgressReporter(self._update)

    def __enter__(self):
        self._started = time.monotonic()
        if self.mode == "auto":
            try:
                terminal = self.console.is_terminal if self.console is not None else self.stream.isatty()
                if terminal:
                    self._start_live()
            except Exception:
                self._stop_live()
            except BaseException:
                self._stop_live()
                raise
        return self

    def _start_live(self):
        from rich.console import Console
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        console = self.console if self.console is not None else Console(file=self.stream)
        if console.is_dumb_terminal:
            return
        self._progress = Progress(
            SpinnerColumn("dots2"),
            TextColumn("Replay · {task.description}", markup=False),
            BarColumn(bar_width=20),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console, refresh_per_second=4,
            redirect_stdout=False, redirect_stderr=False,
        )
        self._task = self._progress.add_task("读取历史轨迹", total=None)
        self._progress.start()

    def __call__(self, event: ProgressEvent):
        self._notify(event)
        if self._notify.callback is None:
            self._stop_live()

    def _update(self, event: ProgressEvent):
        if self.mode == "off":
            return
        if event.total_steps is not None:
            self.total = event.total_steps
        if event.phase == "committed":
            self.completed = event.step
        self.finished = event.phase == "finished"
        message = describe(event)
        if self._progress is not None:
            self._progress.update(self._task, description=message,
                                  total=self.total, completed=self.completed)
            if self.finished:
                self._progress.stop_task(self._task)
                self._progress.refresh()
        else:
            elapsed = time.monotonic() - self._started
            count = f" {self.completed}/{self.total}" if self.total is not None else ""
            print(f"[replay {elapsed:.1f}s{count}] {message}", file=self.stream, flush=True)

    def _stop_live(self):
        progress, self._progress = self._progress, None
        if progress is not None:
            try:
                progress.stop()
            except Exception:
                pass

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is not None and not self.finished:
                self(ProgressEvent("finished",
                                   status="cancelled" if isinstance(exc, KeyboardInterrupt) else "failed"))
        finally:
            self._stop_live()
