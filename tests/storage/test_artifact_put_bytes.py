import hashlib
import os
import stat

import pytest

import palinreplay.storage.artifacts as artifacts
from palinreplay.diagnostics.metrics import measure
from palinreplay.storage.store import IntegrityError, RecordStore
from tests.storage.test_artifact_staging import artifact, corrupt_preserving_mtime


def test_repeat_verifies_existing_bytes_without_content_tempfile(tmp_path, monkeypatch):
    data = b"already stored\x00\xff"
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_bytes(data)
        before = artifact(store, ref).stat()

        def fail(**kwargs):
            pytest.fail("an existing artifact must not create a content tempfile")

        monkeypatch.setattr(artifacts.tempfile, "mkstemp", fail)
        with measure() as metrics:
            assert store.put_bytes(data) == ref
        after = artifact(store, ref).stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
        assert metrics.counters["written_bytes"] == 0
        assert metrics.counters["copied_bytes"] == 0
        assert metrics.counters["read_bytes"] == len(data)
        assert metrics.counters["hashed_bytes"] == 2 * len(data)
        assert metrics.counters["verified_bytes"] == len(data)
        assert metrics.counters["verification_calls"] == 1
        assert metrics.counters["verification_reused"] == 0
        assert metrics.temporary_peak_bytes == 0
        assert "artifact.sync_file" not in metrics.phases
        assert "artifact.sync_directory" in metrics.phases


def test_new_bytes_hash_once_and_sync_file_before_durable_publication(tmp_path, monkeypatch):
    data = b"new content\x00\xff"
    events = []
    fsync, link = os.fsync, os.link
    with RecordStore.create(tmp_path / "store") as store:
        def sync(fd):
            value = os.fstat(fd)
            events.append(("directory" if stat.S_ISDIR(value.st_mode) else "file", value.st_ino))
            return fsync(fd)

        def publish(source, destination, **kwargs):
            events.append(("link", source.stat().st_ino))
            return link(source, destination, **kwargs)

        monkeypatch.setattr(os, "fsync", sync)
        monkeypatch.setattr(os, "link", publish)
        with measure() as metrics:
            ref = store.put_bytes(data)
        destination = artifact(store, ref)
        assert events == [
            ("file", destination.stat().st_ino),
            ("directory", destination.parent.parent.stat().st_ino),
            ("link", destination.stat().st_ino),
            ("directory", destination.parent.stat().st_ino),
        ]
        assert stat.S_IMODE(destination.stat().st_mode) == 0o400
        assert destination.stat().st_nlink == 1
        assert not list((store.root / "artifacts/.tmp").iterdir())
        assert metrics.counters["hashed_bytes"] == len(data)
        assert metrics.counters["written_bytes"] == len(data)
        assert metrics.counters["read_bytes"] == metrics.counters["copied_bytes"] == 0
        assert metrics.temporary_peak_bytes == len(data)
        assert not metrics.temporary
    with RecordStore.open(tmp_path / "store") as reopened:
        assert reopened.read_bytes(ref) == data


def test_failed_file_sync_cleans_stage_without_publishing(tmp_path, monkeypatch):
    with RecordStore.create(tmp_path / "store") as store:
        def fail(fd):
            raise OSError("injected file sync failure")

        monkeypatch.setattr(os, "fsync", fail)
        with measure() as metrics, pytest.raises(OSError, match="sync failure"):
            store.put_bytes(b"not published")
        assert list((store.root / "artifacts").iterdir()) == [store.root / "artifacts/.tmp"]
        assert not list((store.root / "artifacts/.tmp").iterdir())
        assert not metrics.temporary


@pytest.mark.parametrize("data", [b"", b"opaque\x00\xff"])
def test_content_identity_preserves_requested_media_type(tmp_path, data):
    with RecordStore.create(tmp_path / "store") as store:
        first = store.put_bytes(data, media_type="application/octet-stream")
        inode = artifact(store, first).stat().st_ino
        second = store.put_bytes(data, media_type="text/plain")
        assert first.sha256 == second.sha256 == hashlib.sha256(data).hexdigest()
        assert first.size == second.size == len(data)
        assert first.media_type == "application/octet-stream"
        assert second.media_type == "text/plain"
        assert artifact(store, second).stat().st_ino == inode
        changed = store.put_bytes(data + b"!", media_type="text/plain")
        assert changed.sha256 != first.sha256
        assert store.read_bytes(changed) == data + b"!"


def test_repeat_reuses_only_active_boundary_verification(tmp_path):
    data = b"verified in this boundary"
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_bytes(data)
        with store._verification_scope(), measure() as metrics:
            assert store.put_bytes(data) == ref
            assert store.put_bytes(data) == ref
        assert metrics.counters["verification_calls"] == 1
        assert metrics.counters["verification_reused"] == 1
        assert metrics.counters["verified_bytes"] == len(data)
        assert metrics.counters["hashed_bytes"] == 3 * len(data)
        assert metrics.counters["written_bytes"] == metrics.temporary_peak_bytes == 0
        with measure() as next_checks:
            store.put_bytes(data)
            with store._verification_scope():
                store.put_bytes(data)
            with RecordStore.open(store.root, read_only=False) as other:
                other.put_bytes(data)
        assert next_checks.counters["verification_calls"] == 3
        assert next_checks.counters["verified_bytes"] == 3 * len(data)
        assert next_checks.counters["verification_reused"] == 0


@pytest.mark.parametrize("damage", ["same-size", "different-size"])
def test_repeat_rejects_corrupt_target_even_after_scoped_verification(tmp_path, damage):
    data = b"expected content"
    with RecordStore.create(tmp_path / "store") as store, store._verification_scope():
        ref = store.put_bytes(data)
        store.put_bytes(data)
        path = artifact(store, ref)
        if damage == "same-size":
            corrupt_preserving_mtime(path)
        else:
            path.chmod(0o600)
            path.write_bytes(b"damaged")
            path.chmod(0o400)
        damaged = path.read_bytes()
        inode = path.stat().st_ino
        with measure() as metrics, pytest.raises(IntegrityError, match="size/hash mismatch"):
            store.put_bytes(data)
        assert path.read_bytes() == damaged
        assert path.stat().st_ino == inode
        assert metrics.counters["written_bytes"] == metrics.temporary_peak_bytes == 0
        assert metrics.counters["verification_reused"] == 0
        assert not list((store.root / "artifacts/.tmp").iterdir())


@pytest.mark.parametrize("location", ["target", "dangling-target", "shard", "temporary"])
def test_repeat_does_not_follow_symlinks(tmp_path, location):
    data = b"existing content"
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_bytes(data)
        path = artifact(store, ref)
        external = tmp_path / "external"
        external.write_bytes(data)
        if location in {"target", "dangling-target"}:
            path.unlink()
            path.symlink_to(external if location == "target" else tmp_path / "absent")
        elif location == "shard":
            shard = path.parent
            moved = tmp_path / "shard"
            shard.rename(moved)
            shard.symlink_to(moved, target_is_directory=True)
        else:
            temporary = store.root / "artifacts/.tmp"
            temporary.rmdir()
            moved = tmp_path / "temporary"
            moved.mkdir()
            temporary.symlink_to(moved, target_is_directory=True)
        with measure() as metrics, pytest.raises(IntegrityError):
            store.put_bytes(data)
        assert external.read_bytes() == data
        assert metrics.counters["written_bytes"] == metrics.temporary_peak_bytes == 0


@pytest.mark.parametrize("corrupt", [False, True])
def test_publication_race_verifies_winner_without_replacing_it(tmp_path, monkeypatch, corrupt):
    data = b"requested content"
    competitor = tmp_path / "competitor"
    competitor.write_bytes(b"corrupt content" if corrupt else data)
    competitor.chmod(0o400)
    link = os.link
    with RecordStore.create(tmp_path / "store") as store:
        destination = None

        def race(source, target, **kwargs):
            nonlocal destination
            destination = target
            link(competitor, target)
            return link(source, target, **kwargs)

        monkeypatch.setattr(os, "link", race)
        with measure() as metrics:
            if corrupt:
                with pytest.raises(IntegrityError, match="size/hash mismatch"):
                    store.put_bytes(data)
            else:
                assert store.read_bytes(store.put_bytes(data)) == data
        assert destination.stat().st_ino == competitor.stat().st_ino
        assert destination.read_bytes() == competitor.read_bytes()
        assert metrics.counters["verification_calls"] >= 1
        assert not list((store.root / "artifacts/.tmp").iterdir())
        assert not metrics.temporary
