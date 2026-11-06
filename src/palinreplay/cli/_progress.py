"""Shared terminal setup and resource lifetime for CLI progress displays."""

import sys
import time

from palinreplay.core.progress import ProgressEvent, ProgressReporter


class ProgressDisplay:
    _close_on_finish = False

    def __init__(self, mode="auto", *, stream=None, console=None):
        self.mode = mode
        self.stream = stream if stream is not None else sys.stderr
        self.console = console
        self._started = 0.0
        self.finished = False
        self._live = None
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

    def _terminal_console(self):
        from rich.console import Console

        console = self.console if self.console is not None else Console(file=self.stream)
        return None if console.is_dumb_terminal else console

    def __call__(self, event: ProgressEvent):
        self._notify(event)
        if self._notify.callback is None or (self._close_on_finish and self.finished):
            self._stop_live()

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
                self(ProgressEvent(
                    "finished", status="cancelled" if isinstance(exc, KeyboardInterrupt) else "failed",
                ))
        finally:
            self._stop_live()
