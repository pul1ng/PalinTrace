"""Verify external snapshot references without full filesystem scans."""

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar

import palinreplay.storage.rootfs as _rootfs
from palinreplay.core.errors import IntegrityError
from palinreplay.core.models import CaptureInfo
from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.btrfs.models import Snapshot
from palinreplay.storage.btrfs.pool import _ACTIVE, BtrfsPool

_CHECKS = ContextVar("btrfs_material_checks", default=None)


@contextmanager
def verification_scope():
    """Mount each externally referenced pool once per explicit store operation."""
    if _CHECKS.get() is not None:
        yield
        return
    with ExitStack() as stack:
        token = _CHECKS.set(({}, stack))
        try:
            yield
        finally:
            _CHECKS.reset(token)


def verify_state(store, state):
    try:
        snapshot = Snapshot.model_validate(store.read_json(state.manifest))
        key = (snapshot.location.daemon_id, snapshot.location.volume)
        pool = _ACTIVE.get(key)
        if pool is None:
            scope = _CHECKS.get()
            if scope is None:
                with verification_scope():
                    verify_state(store, state)
                return
            pools, stack = scope
            if key not in pools:
                client = connect_docker(600)
                stack.callback(client.close)
                pools[key] = stack.enter_context(BtrfsPool(client, snapshot.location))
            pool = pools[key]
        pool.verify(snapshot)
    except Exception as error:
        raise IntegrityError("Btrfs material identity check failed") from error


def capture_info():
    # Keep the persisted scope unchanged so existing Btrfs nodes remain restorable.
    return CaptureInfo(
        scope=_rootfs.SCOPE["id"],
        included=("/",),
        excluded=tuple(
            "/" + path for path in (
                *_rootfs.SCOPE["exclude_subtrees"], *_rootfs.SCOPE["exclude_paths"],
            )
        ),
        attributes=tuple(_rootfs.SCOPE["attributes"]),
        consistency="step-boundary-nonatomic",
        limitations=(
            "Task processes are paused for a filesystem snapshot; no application/process checkpoint.",
            "Experimental privileged rootfs launcher; not the default Sandbox isolation policy.",
            "Requires the external same-daemon Btrfs pool; portable bundles are unsupported.",
            "No directory/symlink timestamps, subsecond mtime, atime/ctime or inode-number guarantee.",
            "No ACL, xattr, file-capability or sparse-allocation guarantee.",
            "No mounts, volumes, process memory, live services or external effects are restored.",
            "Only explicitly selected public environment values are saved; other values require reinjection.",
            "Restoration creates an idle container, not the original entrypoint or a resumed Agent.",
        ),
        complete=True,
    )
