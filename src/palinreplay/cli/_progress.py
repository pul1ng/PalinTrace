"""Common terminal setup for the three CLI progress displays."""

import sys
import time


class ProgressDisplay:
    def __init__(self, mode="auto", *, stream=None, console=None):
        self.mode = mode
        self.stream = stream if stream is not None else sys.stderr
        self.console = console
        self._started = 0.0
        self.finished = False

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
