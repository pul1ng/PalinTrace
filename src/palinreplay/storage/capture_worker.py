"""A joined acquisition worker; Store operations always stay on the caller thread."""

import signal
import threading
from contextlib import contextmanager
from contextvars import Context
from time import monotonic

from palinreplay.diagnostics.metrics import COUNTERS, count, measure
from palinreplay.sandbox.base import SandboxCleanupError


class _Worker:
    def __init__(self, function, cancel, name):
        self.value = self.error = self.io = None
        self.cancel = cancel
        self.cancelled = False
        self.cancel_error = None
        self.duration_sec = 0.0
        self.done = threading.Event()
        # Do not share mutable Store verification/IO contexts with a worker,
        # including on Python builds which inherit context in new threads.
        self.thread = threading.Thread(target=Context().run, args=(self._run, function),
                                       name=name)

    def _run(self, function):
        started = monotonic()
        with measure() as metrics:
            try:
                self.value = function()
            except BaseException as error:
                self.error = error
            finally:
                self.io = metrics.snapshot()
                self.duration_sec = monotonic() - started
                self.done.set()

    def result(self):
        if self.error is not None:
            raise self.error
        return self.value

    def request_cancel(self):
        while not self.cancelled:
            try:
                self.cancel()
                self.cancelled = True
            except KeyboardInterrupt as error:
                # A repeated signal may interrupt the stop HTTP request. Retry
                # the ownership-checked, idempotent cancellation before joining.
                self.cancel_error = error
            except BaseException as error:
                self.cancel_error = error
                self.cancelled = True

    def wait(self):
        interruption = None
        # Event completion, rather than is_alive(), proves acquisition and its
        # cleanup finished even if a main-thread join was interrupted by SIGINT.
        while not self.done.is_set():
            try:
                self.done.wait(0.05)
            except BaseException as error:
                if interruption is None:
                    interruption = error
                self.request_cancel()
        while True:
            try:
                self.thread.join()
                break
            except BaseException as error:
                if interruption is None:
                    interruption = error
        return interruption


@contextmanager
def capture_worker(function, *, cancel, name="palinreplay-census"):
    worker = _Worker(function, cancel, name)
    primary = None
    started = False
    try:
        # Deliver cancellation after registration so finally can always join
        # the owned thread. This is a tiny section; IO remains interruptible.
        mask = None
        if hasattr(signal, "pthread_sigmask"):
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            worker.thread.start()
            started = True
        finally:
            if mask is not None:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        yield worker
    except BaseException as error:
        primary = error
        raise
    finally:
        if started or worker.thread.ident is not None:
            if primary is not None:
                worker.request_cancel()
            interruption = worker.wait()
            count(**{key: worker.io[key] for key in COUNTERS})
            errors = [e for e in (worker.cancel_error, worker.error) if e is not None]
            secondary = next((e for e in errors if isinstance(e, SandboxCleanupError)),
                             errors[0] if errors else None)
            if secondary is not None:
                for error in errors:
                    if error is not secondary:
                        secondary.add_note(f"additional capture worker failure: {error!r}")
            if isinstance(secondary, SandboxCleanupError):
                if primary is not None:
                    secondary.add_note(f"export also failed: {primary!r}")
                raise secondary
            if primary is not None:
                if secondary is not None:
                    primary.add_note(f"capture worker also failed: {secondary!r}")
                if interruption is not None:
                    primary.add_note(f"cancelled while joining capture worker: {interruption!r}")
            elif interruption is not None:
                if secondary is not None:
                    interruption.add_note(f"capture worker also failed: {secondary!r}")
                raise interruption
