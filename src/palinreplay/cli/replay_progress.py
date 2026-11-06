"""Terminal progress for Replay; only completed historical steps advance the bar."""

import time

from palinreplay.cli._progress import ProgressDisplay
from palinreplay.core.progress import ProgressEvent


def describe(event: ProgressEvent) -> str:
    if event.phase == "source-reference":
        return f"源 C{event.source_step} (引用)"
    position = f"源 C{event.source_step} → C{event.step}"
    if event.phase == "restoring":
        return f"恢复源 C{event.source_step}"
    if event.phase == "replaying":
        return f"执行与保存 {position}"
    if event.phase == "verifying":
        return f"检查源 C{event.source_step} 的 R1 恢复"
    if event.phase == "committed":
        return f"已完成 {position}"
    if event.phase == "finished":
        return event.label or event.status.upper()
    return {
        "preparing": "读取历史轨迹",
        "connecting": "连接 Docker",
        "service-preparation": "准备指定服务",
        "cleanup": "清理临时资源",
    }[event.phase]


class ReplayProgressDisplay(ProgressDisplay):
    """Live progress on stderr, plain stage updates when redirected."""

    def __init__(self, mode="auto", *, stream=None, console=None):
        super().__init__(mode, stream=stream, console=console)
        self.completed = 0
        self.total = None

    def _start_live(self):
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        console = self._terminal_console()
        if console is None:
            return
        self._live = Progress(
            SpinnerColumn("dots2"),
            TextColumn("Replay · {task.description}", markup=False),
            BarColumn(bar_width=20),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console, refresh_per_second=4,
            redirect_stdout=False, redirect_stderr=False,
        )
        self._task = self._live.add_task("读取历史轨迹", total=None)
        self._live.start()

    def _update(self, event: ProgressEvent):
        if event.total_steps is not None:
            self.total = event.total_steps
        if event.completed_steps is not None:
            self.completed = event.completed_steps
        self.finished = event.phase == "finished"
        if self.mode == "off":
            return
        message = describe(event)
        if self._live is not None:
            self._live.update(self._task, description=message,
                                  total=self.total, completed=self.completed)
            if self.finished:
                self._live.stop_task(self._task)
                self._live.refresh()
        else:
            elapsed = time.monotonic() - self._started
            count = f" {self.completed}/{self.total}" if self.total is not None else ""
            print(f"[replay {elapsed:.1f}s{count}] {message}", file=self.stream, flush=True)
