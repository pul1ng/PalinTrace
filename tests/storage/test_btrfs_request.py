import io
import json
import tarfile
from types import SimpleNamespace
from uuid import uuid4

import pytest

from palinreplay.sandbox.base import SandboxCleanupError
from palinreplay.storage.btrfs import worker
from palinreplay.storage.btrfs.models import PoolLocation
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.store import IntegrityError


def pool_with_transport(monkeypatch, tmp_path, *, failure=None, cleanup_fails=False, rejected=False):
    identity = uuid4()
    pool = BtrfsPool(None, PoolLocation(
        volume="palinreplay-btrfs-" + identity.hex, uuid=identity, daemon_id="fixture",
        helper_image_id="sha256:" + "a" * 64,
    ))
    monkeypatch.setattr(worker, "REQUESTS", tmp_path)
    uploaded, commands, requests = [], [], []

    def upload(directory, data):
        assert directory == "/tmp"
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            members = archive.getmembers()
            assert len(members) == 1 and members[0].mode == 0o600
            member = members[0]
            assert "/" not in member.name
            (tmp_path / member.name).write_bytes(archive.extractfile(member).read())
            uploaded.append(member.name)
        return not rejected

    def execute(argv):
        commands.append(argv)
        assert len(json.dumps(argv).encode()) < 1024
        if argv[0] == "rm":
            if cleanup_fails:
                return SimpleNamespace(exit_code=1, output=b"cleanup failed")
            (tmp_path / argv[-1].rsplit("/", 1)[-1]).unlink(missing_ok=True)
            return SimpleNamespace(exit_code=0, output=b"")
        if failure == "startup":
            raise RuntimeError("exec startup failed")
        request = worker.read_request(argv[2:])
        requests.append(request)
        if failure == "worker":
            return SimpleNamespace(exit_code=1, output=b'{"ok":false,"error":"comparison failed"}')
        return SimpleNamespace(exit_code=0, output=b'{"ok":true,"result":{"equal":true}}')

    pool.container = SimpleNamespace(put_archive=upload, exec_run=execute)
    return pool, uploaded, commands, requests


def test_large_candidate_union_is_transferred_outside_argv(tmp_path, monkeypatch):
    pool, uploaded, commands, requests = pool_with_transport(monkeypatch, tmp_path)
    names = [f"old/{i:04}-" + "x" * 70 for i in range(1800)]
    names += [name.replace("old/", "new/", 1) for name in names]
    names += ["quoted\"file", "line\nbreak", "non-ascii-文件"]
    assert len(json.dumps(names).encode()) > 200_000
    before, after = object(), SimpleNamespace(name="checkpoints/" + uuid4().hex)
    monkeypatch.setattr(pool, "changes", lambda a, b: {"candidates": names})
    result = pool.compare_incremental(before, after, before, after)
    assert result["equal"]
    assert requests[0]["op"] == "compare_candidates"
    assert requests[0]["candidates"] == sorted(set(names))
    assert commands[0][2:] == ["--request-file", uploaded[0]]
    assert len(commands) == 2 and not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", ["startup", "worker"])
def test_request_cleanup_preserves_comparison_error(tmp_path, monkeypatch, failure):
    pool, _, _, _ = pool_with_transport(monkeypatch, tmp_path, failure=failure)
    error = RuntimeError if failure == "startup" else IntegrityError
    with pytest.raises(error):
        pool._file_command("compare_candidates", candidates=["file"])
    assert not list(tmp_path.iterdir())


def test_cleanup_failure_does_not_mask_worker_error(tmp_path, monkeypatch):
    pool, _, _, _ = pool_with_transport(monkeypatch, tmp_path, failure="worker", cleanup_fails=True)
    with pytest.raises(IntegrityError, match="comparison failed") as raised:
        pool._file_command("compare_candidates", candidates=[])
    assert "request cleanup also failed" in raised.value.__notes__[0]


def test_cleanup_failure_after_success_is_reported(tmp_path, monkeypatch):
    pool, _, _, _ = pool_with_transport(monkeypatch, tmp_path, cleanup_fails=True)
    with pytest.raises(SandboxCleanupError, match="removal unconfirmed"):
        pool._file_command("compare_candidates", candidates=[])


def test_failed_upload_cleans_partially_transferred_request(tmp_path, monkeypatch):
    pool, _, commands, _ = pool_with_transport(monkeypatch, tmp_path, rejected=True)
    with pytest.raises(RuntimeError, match="transfer was rejected"):
        pool._file_command("compare_candidates", candidates=[])
    assert commands[0][0] == "rm" and not list(tmp_path.iterdir())


def test_invalid_request_file_is_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "REQUESTS", tmp_path)
    name = "palin-btrfs-request-" + uuid4().hex + ".json"
    (tmp_path / name).write_bytes(b"truncated {")
    with pytest.raises(ValueError):
        worker.read_request(["--request-file", name])
    assert not (tmp_path / name).exists()
    with pytest.raises(ValueError, match="arguments"):
        worker.read_request(["--request-file", "../source.json"])
