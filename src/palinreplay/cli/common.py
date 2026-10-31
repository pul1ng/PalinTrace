"""Cancellation handling shared by command entry points."""

import signal
import threading
from contextlib import contextmanager


@contextmanager
def cancellation_signals():
    """Let native cancellation/finally handlers run for SIGTERM as for Ctrl-C."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def cancel(signum, frame):
        raise KeyboardInterrupt()

    previous = signal.signal(signal.SIGTERM, cancel)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
