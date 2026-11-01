"""Borrow an R1 session's Docker execution path without creating resources."""

import threading

from palinreplay.core.errors import IntegrityError
from palinreplay.sandbox.btrfs import BtrfsSandbox
from palinreplay.sandbox.docker import DockerSDKSandbox


class RestoredExecutor:
    # Keep output capture, definitive-result checks and deadline handling identical
    # to Record. Only lifetime ownership differs.
    exec = DockerSDKSandbox.exec
    check_root = BtrfsSandbox.check_root

    def __init__(self, restored, runtime):
        self.restored, self.runtime = restored, runtime
        self.pool, self.workspace = restored.pool, restored.workspace.name
        self._container = restored.container
        self._lock = threading.RLock()
        self._workdir = runtime.launch.workdir
        self._shell = runtime.launch.shell
        self._env = dict(runtime.launch.environment)
        self._timeout = 60
        self.name = restored.workspace.container_name
        self.state = "running"
        self.mounts_at_start = None
        self.check_root()

    def execute(self, request):
        if tuple(request["interpreter"]) != tuple(self._shell):
            raise IntegrityError("historical interpreter differs from the restored launch shell")
        return self.exec(request["action"]["command"], cwd=request["cwd"],
                         env=request["env"], timeout_sec=request["timeout_sec"])

    def _cleanup_after_error(self, original):
        try:
            self.restored.close()
        except BaseException as cleanup:
            original.add_note(f"restore cleanup also failed: {cleanup!r}")
        finally:
            self.state = "failed"

    def close(self):
        # A borrowed adapter never closes its session or client on normal exit.
        self.state = "closed"
