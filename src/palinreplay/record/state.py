"""Capture one completed recording boundary as a Btrfs snapshot."""

import time

from palinreplay.core.models import State, StateCapture
from palinreplay.sandbox.base import EnvironmentMismatch
from palinreplay.sandbox.runtime import _environment
from palinreplay.storage.btrfs.models import BACKEND, BtrfsRuntime, Snapshot
from palinreplay.storage.btrfs.verification import capture_info


class BtrfsStateBackend:
    def __init__(self, sandbox, *, public_env_keys, runtime=None):
        self.sandbox = sandbox
        self.public_env_keys = frozenset(public_env_keys)
        self.runtime = runtime

    def capture(self, store, boundary, previous):
        started = time.monotonic()
        sandbox, pool = self.sandbox, self.sandbox.pool
        container = sandbox._container
        sandbox.check_root()
        if boundary.index == 0 and self.runtime is None:
            snapshot = sandbox.preload.base.metadata.snapshot
            pool.verify(snapshot)
        else:
            container.pause()
            try:
                name = "checkpoints/" + boundary.node_id
                saved = pool.snapshot(sandbox.workspace, name)
                snapshot = Snapshot(location=pool.location, name=name, uuid=saved["uuid"])
            finally:
                container.unpause()
        container.reload()
        actual = _environment(container.attrs["Config"])
        if self.public_env_keys - actual.keys():
            raise EnvironmentMismatch("public environment keys missing from Btrfs container")
        base = self.runtime if self.runtime is not None else sandbox.preload.base.metadata
        launch = base.launch.model_copy(
            update={
                "environment": {k: actual[k] for k in sorted(self.public_env_keys)},
                "omitted_environment_keys": tuple(sorted(actual.keys() - self.public_env_keys)),
                "network_mode": base.launch.network_mode if self.runtime is not None else sandbox._network,
            }
        )
        runtime = BtrfsRuntime(
            source_container_id=container.id,
            source_image_id=base.source_image_id,
            bootstrap_image_id=base.bootstrap_image_id,
            launch=launch,
            c0_oracle_sha256=(base.c0_oracle_sha256 if self.runtime is not None else base.oracle_sha256),
        )
        manifest = store.put_json(snapshot.model_dump(mode="json"))
        state = store.save(
            State(
                backend=BACKEND,
                capture=capture_info(),
                manifest=manifest,
                runtime=store.put_json(runtime.model_dump(mode="json")),
                artifacts=(manifest,),
                logical_parent_id=previous.state_id if previous else None,
            )
        )
        details = {
            "capture_mode": "btrfs-cow-snapshot",
            "duration_sec": time.monotonic() - started,
            "docker_exports": 0,
            "archive_bytes_scanned": 0,
            "full_tree_scans": 0,
            "capture_complete": True,
            "snapshot": snapshot.model_dump(mode="json"),
            "storage": pool.command("usage"),
        }
        store.save(
            StateCapture(
                run_id=boundary.run_id,
                attempt_id=boundary.attempt_id,
                step_id=boundary.step_id,
                node_id=boundary.node_id,
                index=boundary.index,
                state_id=state.state_id,
                details=store.put_json(details),
            )
        )
        return state
