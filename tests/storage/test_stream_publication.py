import stat
from dataclasses import replace

import pytest

import palinreplay.storage.rootfs as _rootfs
from palinreplay.core.models import ArtifactRef
from palinreplay.diagnostics.metrics import measure
from palinreplay.storage.store import IntegrityError, RecordStore, StoreError
from tests.storage.test_artifact_staging import artifact, corrupt_preserving_mtime
from tests.storage.test_rootfs_manifest import files, tar_bytes
from tests.storage.test_rootfs_stream import Export


def inventory(path):
    return _rootfs.export_inventory(Export(tar_bytes(files())), path)


def publish(store, path, inv):
    return store._publish_inventoried_file(path, inv, media_type="application/octet-stream")


def test_one_final_byte_scan_and_only_boundary_local_reuse(tmp_path):
    with RecordStore.create(tmp_path / "store") as store, store.stage_file() as path:
        inv = inventory(path)
        inode = path.stat().st_ino
        with store._verification_scope(), measure() as metrics:
            ref = publish(store, path, inv)
            assert not path.exists()
            final = artifact(store, ref).stat()
            assert final.st_ino == inode and final.st_nlink == 1
            assert stat.S_IMODE(final.st_mode) == 0o400
            store._verify_artifact(ref)
            store._verify_artifact(ref)
        assert metrics.counters["read_bytes"] == inv.size
        assert metrics.counters["verified_bytes"] == inv.size
        assert metrics.counters["hashed_bytes"] == inv.size
        assert metrics.counters["verification_reused"] == 2
        assert "artifact.hash_staged" not in metrics.phases
        assert not store._artifacts._streamed
        with measure() as checks:
            store.verify_artifact(ref)
            with store._verification_scope():
                store._verify_artifact(ref)
            with RecordStore.open(store.root) as reopened:
                reopened.verify_artifact(ref)
        assert checks.counters["verified_bytes"] == 3 * inv.size


def test_generic_publication_still_hashes_staging_and_verifies_references(tmp_path):
    with RecordStore.create(tmp_path / "store") as store, store.stage_file() as path:
        inv = inventory(path)
        with store._verification_scope(), measure() as metrics:
            ref = store.publish_staged_file(path, media_type="application/octet-stream")
            store._verify_artifact(ref)
        assert metrics.counters["read_bytes"] == 2 * inv.size
        assert "artifact.hash_staged" in metrics.phases


def test_receipt_requires_ownership_and_is_cleared_on_abandonment(tmp_path):
    with RecordStore.create(tmp_path / "a") as store, RecordStore.create(tmp_path / "b") as other:
        with store.stage_file() as path:
            inv = inventory(path)
            with pytest.raises(StoreError, match="active staging"):
                publish(other, path, inv)
            expected = ArtifactRef(sha256=inv.sha256, size=inv.size, media_type="application/octet-stream")
            with store._artifacts._expect_stream(path, expected, inv.identity):
                with pytest.raises(StoreError, match="exclusive"):
                    with store._artifacts._expect_stream(path, expected, inv.identity):
                        pytest.fail("a pending receipt cannot be replaced")
            assert not store._artifacts._streamed
            with measure() as metrics:
                store.publish_staged_file(path, media_type="application/octet-stream")
            assert "artifact.hash_staged" in metrics.phases
            with pytest.raises(StoreError):
                store.publish_staged_file(path, media_type="application/octet-stream")


@pytest.mark.parametrize("fault", ["missing-digest", "wrong-digest", "wrong-size", "changed-stage"])
def test_expectation_is_not_a_substitute_for_saved_byte_validation(tmp_path, fault):
    with RecordStore.create(tmp_path / "store") as store, store.stage_file() as path:
        inv = inventory(path)
        if fault == "missing-digest":
            inv = replace(inv, sha256=None)
        elif fault == "wrong-digest":
            inv = replace(inv, sha256="f" * 64)
        elif fault == "wrong-size":
            inv = replace(inv, size=inv.size + 1)
        else:
            corrupt_preserving_mtime(path)
        with store._verification_scope(), pytest.raises((IntegrityError, ValueError)):
            publish(store, path, inv)
        assert not store._artifacts._streamed
        assert store._artifacts._checks.get() is None


@pytest.mark.parametrize("damaged", [False, True])
def test_existing_destination_is_independently_checked_and_never_overwritten(tmp_path, damaged):
    with RecordStore.create(tmp_path / "store") as store, store.stage_file() as path:
        inv = inventory(path)
        old = store.put_bytes(path.read_bytes(), media_type="application/octet-stream")
        destination = artifact(store, old)
        inode = destination.stat().st_ino
        with store._verification_scope():
            store._verify_artifact(old)
            if damaged:
                corrupt_preserving_mtime(destination)
                original = destination.read_bytes()
                with pytest.raises(IntegrityError, match="size/hash mismatch"):
                    publish(store, path, inv)
                assert destination.read_bytes() == original
            else:
                with measure() as metrics:
                    assert publish(store, path, inv) == old
                assert metrics.counters["verified_bytes"] == inv.size
            assert destination.stat().st_ino == inode


@pytest.mark.parametrize("scope", ["none", "unreliable-clock"])
def test_unavailable_boundary_proofs_fall_back_to_full_checks(tmp_path, monkeypatch, scope):
    with RecordStore.create(tmp_path / "store") as store, store.stage_file() as path:
        inv = inventory(path)
        monkeypatch.setattr(store._artifacts, "_clock_advanced", lambda *args: False)
        context = (store._verification_scope() if scope == "unreliable-clock"
                   else store._artifacts.strict_checks())
        with context, measure() as metrics:
            ref = publish(store, path, inv)
            store._verify_artifact(ref)
        assert metrics.counters["verified_bytes"] == 2 * inv.size
        assert metrics.counters["verification_reused"] == 0
