"""Resume progress: saved branch steps against a budget, not task completion."""

import sys
import time

from palinreplay.core.progress import ProgressEvent, ProgressReporter


def describe(event: ProgressEvent) -> str:
    if event.phase == "restoring":
        return f"恢复源 C{event.source_step}"
    if event.phase == "verifying":
        return f"检查源 C{event.source_step} 的 R1 恢复"
    if event.phase == "source-reference":
        return f"源 C{event.source_step} 已就绪"
    if event.phase == "model":
        return f"C{event.step} · 等待模型回答 (第 {event.request} 次请求)"
    if event.phase == "model_returned":
        return f"C{event.step} · 已收到模型回答"
    if event.phase in ("tool", "tool_returned"):
        action = "执行工具" if event.phase == "tool" else "工具已返回"
        return f"C{event.step} · {action} {event.tool}/{event.tools}"
    if event.phase in ("capturing", "committing", "committed"):
        action = {"capturing": "保存文件快照", "committing": "提交节点",
                  "committed": "节点已保存"}[event.phase]
        return f"C{event.step} · {action}"
    if event.phase == "finished":
        return {
            "submitted": "模型已提交",
            "budget_exhausted": "新增预算耗尽, 已暂停",
            "incomplete": "工具组未完成, 已保留实际结果",
            "cancelled": "已取消",
            "failed": "运行失败, 请查看报告",
        }.get(event.status, "运行结束")
    return {
        "preparing": "读取配置与来源轨迹",
        "connecting": "连接 Docker",
        "service-preparation": "准备起点所需服务",
        "cleanup": "清理临时资源",
    }[event.phase]


class ResumeProgressDisplay:
    """Best-effort live view on stderr; redirected output stays plain text."""

    def __init__(self, max_steps, mode="auto", *, stream=None, console=None):
        self.max_steps, self.mode = max_steps, mode
        self.stream = stream if stream is not None else sys.stderr
        self.console = console
        self.completed = 0
        self.last_node = "尚无"
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
            TextColumn("Resume · {task.description}", markup=False),
            BarColumn(bar_width=20),
            MofNCompleteColumn(),
            TextColumn("已保存/预算 · {task.fields[node]}", markup=False),
            TimeElapsedColumn(),
            console=console, refresh_per_second=4,
            # Keep terminal messages above the live bar; redirected stdout
            # retains the CLI's normal output stream.
            redirect_stdout=sys.stdout.isatty(), redirect_stderr=True,
        )
        self._task = self._progress.add_task(
            "读取配置与来源轨迹", total=self.max_steps, node=self.last_node,
        )
        self._progress.start()

    def __call__(self, event: ProgressEvent):
        self._notify(event)
        if self._notify.callback is None or self.finished:
            self._stop_live()

    def _update(self, event: ProgressEvent):
        if event.source_step is not None and not self.completed:
            self.last_node = f"源 C{event.source_step} (引用)"
        if event.completed_steps is not None:
            self.completed = event.completed_steps
        if event.phase in ("committed", "finished") and event.step is not None:
            self.last_node = f"C{event.step}"
        self.finished = event.phase == "finished"
        if self.mode == "off":
            return
        message = describe(event)
        if self._progress is not None:
            self._progress.update(self._task, description=message,
                                  completed=self.completed, node=self.last_node)
            if self.finished:
                self._progress.stop_task(self._task)
                self._progress.refresh()
        else:
            elapsed = time.monotonic() - self._started
            print(f"[resume {elapsed:.1f}s {self.completed}/{self.max_steps} 已保存/预算"
                  f" · {self.last_node}] {message}", file=self.stream, flush=True)

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
