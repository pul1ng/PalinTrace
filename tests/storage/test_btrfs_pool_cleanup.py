from types import SimpleNamespace
from uuid import uuid4

import pytest

import palinreplay.storage.btrfs.pool as pools
from palinreplay.sandbox.base import SandboxCleanupError
from palinreplay.storage.btrfs.models import PoolLocation
from palinreplay.storage.btrfs.pool import OWNER_LABEL, POOL_LABEL, BtrfsPool


def missing_helper():
    error = RuntimeError("fixture helper no longer exists")
    error.status_code = 404
    return error


@pytest.fixture
def cleanup_pool(monkeypatch):
    state = SimpleNamespace(
        removed=False, reloads=0, removes=0, closes=0, probes=0,
        probe_failures=0, lookup_failures=0, lookups=0, volume_removed=False,
    )
    identity = uuid4()
    location = PoolLocation(
        volume="palinreplay-btrfs-" + identity.hex, uuid=identity,
        daemon_id="fixture", helper_image_id="sha256:" + "a" * 64,
    )

    def reload():
        state.reloads += 1
        if state.removed:
            raise missing_helper()

    def remove(**kwargs):
        assert kwargs == {"force": True, "v": True}
        assert not state.removed
        state.removes += 1
        state.removed = True

    def get(identifier):
        assert identifier == "fixture-helper"
        state.lookups += 1
        if state.lookup_failures:
            state.lookup_failures -= 1
            raise OSError("helper absence check unavailable")
        if state.removed:
            raise missing_helper()
        return container

    def probe(*args, **kwargs):
        state.probes += 1
        if state.probe_failures:
            state.probe_failures -= 1
            raise RuntimeError("loop release check failed")
        return b""

    def remove_volume():
        assert pool.closed
        state.volume_removed = True

    volume = SimpleNamespace(
        attrs={"Labels": {POOL_LABEL: str(identity)}}, remove=remove_volume,
    )
    client = SimpleNamespace(
        containers=SimpleNamespace(get=get, run=probe),
        volumes=SimpleNamespace(get=lambda name: volume),
    )
    pool = BtrfsPool(client, location)
    container = SimpleNamespace(
        id="fixture-helper", reload=reload, remove=remove,
        attrs={"Config": {"Labels": {OWNER_LABEL: pool.owner}}},
    )
    pool.container, pool.started, pool.device = container, True, "/dev/loop-fixture"

    def close_worker(op):
        assert op == "close"
        state.closes += 1

    pool.command = close_worker
    monkeypatch.setitem(pools._ACTIVE, pool.key, pool)
    return pool, state


def test_successful_close_is_idempotent(cleanup_pool):
    pool, state = cleanup_pool
    pool.close()
    pool.close()
    assert pool.closed and pool.key not in pools._ACTIVE
    assert (state.reloads, state.closes, state.removes, state.probes) == (1, 1, 1, 1)


def test_repeat_close_rechecks_release_without_reloading_removed_helper(cleanup_pool):
    pool, state = cleanup_pool
    state.probe_failures = 1
    with pytest.raises(SandboxCleanupError, match="loop device release unconfirmed"):
        pool.close()
    assert state.removed and not pool.closed
    assert pools._ACTIVE[pool.key] is pool
    pool.close()
    assert pool.closed and pool.key not in pools._ACTIVE
    assert (state.reloads, state.closes, state.removes, state.probes) == (1, 1, 1, 2)


def test_discard_after_partial_close_verifies_release_before_removing_volume(cleanup_pool):
    pool, state = cleanup_pool
    state.probe_failures = 1
    with pytest.raises(SandboxCleanupError):
        pool.close()
    pool.discard()
    assert state.volume_removed and pool.closed
    assert (state.reloads, state.removes, state.probes) == (1, 1, 2)


def test_persistent_release_failure_stays_failed_without_discarding_volume(cleanup_pool):
    pool, state = cleanup_pool
    state.probe_failures = 2
    with pytest.raises(SandboxCleanupError):
        pool.close()
    with pytest.raises(SandboxCleanupError, match="loop device release unconfirmed"):
        pool.discard()
    assert not state.volume_removed and not pool.closed
    assert pools._ACTIVE[pool.key] is pool
    assert state.reloads == state.removes == 1 and state.probes == 2


def test_absence_check_failure_does_not_repeat_helper_removal(cleanup_pool):
    pool, state = cleanup_pool
    state.lookup_failures = 1
    with pytest.raises(OSError, match="absence check"):
        pool.close()
    assert state.removed and not pool.closed and state.probes == 0
    pool.close()
    assert pool.closed and state.lookups == 2
    assert (state.reloads, state.closes, state.removes, state.probes) == (1, 1, 1, 1)


def test_close_still_refuses_unowned_helper(cleanup_pool):
    pool, state = cleanup_pool
    pool.container.attrs["Config"]["Labels"][OWNER_LABEL] = "another-owner"
    with pytest.raises(SandboxCleanupError, match="unowned"):
        pool.close()
    assert not state.removed and not pool.closed
    assert state.closes == state.removes == state.probes == 0
