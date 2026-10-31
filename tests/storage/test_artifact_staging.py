import os
import stat

import pytest

from palinreplay.diagnostics.metrics import measure
from palinreplay.storage.store import IntegrityError, RecordStore, StoreError


def artifact(store, ref):
    return store.root / "artifacts" / ref.sha256[:2] / ref.sha256[2:]


def corrupt_preserving_mtime(path):
    before = path.stat()
    path.chmod(0o600)
    with path.open("r+b") as handle:
        value = handle.read(1)
        handle.seek(0)
        handle.write(bytes([value[0] ^ 0xFF]))
    path.chmod(stat.S_IMODE(before.st_mode))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_direct_publication_consumes_path_without_copying_and_survives_reopen(tmp_path):
    with RecordStore.create(tmp_path / "store") as store, measure() as metrics:
        with store.stage_file() as path:
            assert not path.exists()
            path.write_bytes(b"complete\x00\xff")
            inode = path.stat().st_ino
            ref = store.publish_staged_file(path)
            assert not path.exists()
            assert artifact(store, ref).stat().st_ino == inode
            assert stat.S_IMODE(artifact(store, ref).stat().st_mode) == 0o400
            with pytest.raises(StoreError):
                store.publish_staged_file(path)
        assert not list((store.root / "artifacts/.tmp").iterdir())
        assert metrics.snapshot()["copied_bytes"] == 0
        assert metrics.snapshot()["hashed_bytes"] == ref.size
    with RecordStore.open(tmp_path / "store") as reader:
        assert reader.read_bytes(ref) == b"complete\x00\xff"
        with pytest.raises(StoreError), reader.stage_file():
            pytest.fail("read-only stores cannot stage files")


@pytest.mark.parametrize("kind", ["absent", "directory", "symlink", "hardlink", "fifo"])
def test_invalid_staging_never_modifies_external_files(tmp_path, kind):
    external = tmp_path / "external"
    external.write_bytes(b"keep")
    mode = external.stat().st_mode
    with RecordStore.create(tmp_path / "store") as store:
        with pytest.raises(StoreError):
            store.publish_staged_file(external)
        with store.stage_file() as path:
            if kind == "directory":
                path.mkdir()
            elif kind == "symlink":
                path.symlink_to(external)
            elif kind == "hardlink":
                os.link(external, path)
            elif kind == "fifo":
                os.mkfifo(path)
            with pytest.raises(IntegrityError):
                store.publish_staged_file(path)
        assert not list((store.root / "artifacts/.tmp").iterdir())
    assert external.read_bytes() == b"keep" and external.stat().st_mode == mode


def test_staging_ownership_parent_identity_and_exception_cleanup(tmp_path):
    with RecordStore.create(tmp_path / "one") as first, RecordStore.create(tmp_path / "two") as other:
        with first.stage_file() as path:
            path.write_bytes(b"complete")
            with pytest.raises(StoreError):
                other.publish_staged_file(path)
            saved = path.parent.with_name("moved")
            path.parent.rename(saved)
            try:
                path.parent.mkdir()
                path.write_bytes(b"replaced")
                with pytest.raises(IntegrityError):
                    first.publish_staged_file(path)
            finally:
                path.unlink()
                path.parent.rmdir()
                saved.rename(path.parent)
        with pytest.raises(KeyboardInterrupt), first.stage_file() as path:
            path.write_bytes(b"partial")
            raise KeyboardInterrupt()
        assert not list((first.root / "artifacts/.tmp").iterdir())


def test_publication_fsync_failure_and_damaged_deduplication_fail_closed(tmp_path, monkeypatch):
    with RecordStore.create(tmp_path / "store") as store:
        with store.stage_file() as path:
            path.write_bytes(b"complete")
            with monkeypatch.context() as patch:
                def fail(fd):
                    raise OSError("injected sync failure")
                patch.setattr(os, "fsync", fail)
                with pytest.raises(OSError):
                    store.publish_staged_file(path)
        assert list((store.root / "artifacts").iterdir()) == [store.root / "artifacts/.tmp"]
        good = store.put_bytes(b"complete")
        corrupt_preserving_mtime(artifact(store, good))
        corrupt = artifact(store, good).read_bytes()
        with store.stage_file() as path:
            path.write_bytes(b"complete")
            with pytest.raises(IntegrityError):
                store.publish_staged_file(path)
        assert artifact(store, good).read_bytes() == corrupt


def test_proofs_are_private_to_one_scope_and_public_reads_still_hash(tmp_path):
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_bytes(b"12345")
        with store._verification_scope(), measure() as metrics:
            store._verify_artifact(ref)
            store._verify_artifact(ref)
            assert metrics.snapshot()["verified_bytes"] == 5
            assert metrics.snapshot()["verification_reused"] == 1
            store.verify_artifact(ref)
            store.read_bytes(ref)
            store.export_artifact(ref, tmp_path / "exported")
            assert metrics.snapshot()["verified_bytes"] == 20
            with RecordStore.open(store.root, read_only=False) as second:
                second._verify_artifact(ref)
            assert metrics.snapshot()["verified_bytes"] == 25
        with measure() as next_scope:
            store._verify_artifact(ref)
            with store._verification_scope():
                store._verify_artifact(ref)
        assert next_scope.snapshot()["verified_bytes"] == 10
        assert next_scope.snapshot()["verification_reused"] == 0


@pytest.mark.parametrize("damage", ["write-restore-mtime", "inode", "missing", "symlink"])
def test_proof_never_masks_changed_contents_or_identity(tmp_path, damage):
    with RecordStore.create(tmp_path / "store") as store, store._verification_scope():
        ref = store.put_bytes(b"12345")
        path = artifact(store, ref)
        store._verify_artifact(ref)
        if damage == "write-restore-mtime":
            corrupt_preserving_mtime(path)
        elif damage == "inode":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(b"XXXXX")
            replacement.chmod(0o400)
            os.replace(replacement, path)
        else:
            path.unlink()
            if damage == "symlink":
                external = tmp_path / "external"
                external.write_bytes(b"12345")
                path.symlink_to(external)
        with pytest.raises(IntegrityError):
            store._verify_artifact(ref)


@pytest.mark.parametrize("fallback", ["coarse-clock", "probe-unavailable", "writable", "aliased"])
def test_unreliable_or_mutable_files_are_fully_verified(tmp_path, monkeypatch, fallback):
    import palinreplay.storage.artifacts as module

    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_bytes(b"12345")
        if fallback == "coarse-clock":
            monkeypatch.setattr(store._artifacts, "_clock_advanced", lambda *a: False)
        elif fallback == "probe-unavailable":
            def fail(**kwargs):
                raise OSError("probe unavailable")
            monkeypatch.setattr(module.tempfile, "TemporaryFile", fail)
        elif fallback == "writable":
            artifact(store, ref).chmod(0o600)
        else:
            os.link(artifact(store, ref), tmp_path / "alias")
        with store._verification_scope(), measure() as metrics:
            store._verify_artifact(ref)
            store._verify_artifact(ref)
        assert metrics.snapshot()["verified_bytes"] == 10
        assert metrics.snapshot()["verification_reused"] == 0


def test_changed_file_during_verification_cannot_issue_a_proof(tmp_path, monkeypatch):
    with RecordStore.create(tmp_path / "store") as store, store._verification_scope():
        ref = store.put_bytes(b"12345")
        verify = store._artifacts.verify

        def race(value):
            verify(value)
            corrupt_preserving_mtime(artifact(store, value))

        monkeypatch.setattr(store._artifacts, "verify", race)
        with pytest.raises(IntegrityError, match="changed during verification"):
            store._verify_artifact(ref)
        assert not store._artifacts._checks.get().verified


def test_clock_becoming_unreliable_revokes_all_earlier_proofs(tmp_path, monkeypatch):
    with RecordStore.create(tmp_path / "store") as store, store._verification_scope():
        first, second = store.put_bytes(b"first"), store.put_bytes(b"second")
        store._verify_artifact(first)
        store._verify_artifact(second)
        monkeypatch.setattr(store._artifacts, "_clock_advanced", lambda *a: False)
        with measure() as metrics:
            store._verify_artifact(first)
            store._verify_artifact(second)
        assert metrics.snapshot()["verified_bytes"] == first.size + second.size
        assert metrics.snapshot()["verification_reused"] == 0
        assert not store._artifacts._checks.get().verified
