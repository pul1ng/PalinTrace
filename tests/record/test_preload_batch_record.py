import json

import pytest

from palinreplay.cli import main
from palinreplay.core.errors import IntegrityError, RecordUsageError
from palinreplay.preload.base import BtrfsPreloadRecord, open_preload
from palinreplay.record.run import RunOptions, run_record
from palinreplay.sandbox.binding import load_sandbox_task
from palinreplay.storage.btrfs.pool import BtrfsPool
from tests.agents.test_mini_agent import Transport, response
from tests.agents.test_mini_agent import deterministic as deterministic
from tests.cli.test_preload_cli import cli_runtime as cli_runtime
from tests.preload.test_preload import runtime as runtime
from tests.preload.test_preload_batch import batch_package as batch_package
from tests.record import test_record_run

record_runtime = test_record_run.runtime

# The existing Record runtime fixture fakes selection; this test restores the real method.
REAL_SELECT = BtrfsPreloadRecord.__dict__["select"]


def test_batch_children_bind_and_start_record_independently(
    tmp_path, batch_package, cli_runtime, record_runtime, deterministic, monkeypatch,
):
    batch = tmp_path / "batch"
    selected = ["example__repo-1", "example__repo-2"]
    assert main([
        "preload", str(batch_package), "--output", str(batch),
        "--task-id", selected[0], "--task-id", selected[1],
    ]) == 0
    single = tmp_path / "single"
    assert main([
        "preload", str(batch_package), "--output", str(single), "--task-id", selected[0],
    ]) == 0
    monkeypatch.setattr(BtrfsPreloadRecord, "select", REAL_SELECT)
    index = json.loads((batch / "preloads.json").read_text())
    bindings = [load_sandbox_task(batch_package, task_id) for task_id in selected]
    snapshots = []
    for entry, binding in zip(index["tasks"], bindings, strict=True):
        child = batch / entry["path"]
        base = open_preload(child)
        assert entry["preload_id"] == "preload:sha256:" + base.metadata_sha256
        assert BtrfsPreloadRecord.select(child, binding).base == base
        snapshots.append(base.metadata.snapshot)
        pool = BtrfsPool(None, base.metadata.snapshot.location)
        pool.command = lambda op, **kwargs: {
            "uuid": str(base.metadata.snapshot.uuid), "readonly": True,
        }
        pool.verify(base.metadata.snapshot)

    assert snapshots[0].location != snapshots[1].location
    assert snapshots[0].uuid != snapshots[1].uuid
    with pytest.raises(RecordUsageError, match="input differs"):
        BtrfsPreloadRecord.select(batch / selected[0], bindings[1])
    with pytest.raises(IntegrityError, match="another pool"):
        pool.verify(snapshots[0])
    with pytest.raises((FileNotFoundError, IntegrityError)):
        open_preload(batch)

    for number, (task_id, preload) in enumerate([
        (selected[0], batch / selected[0]), (selected[1], batch / selected[1]),
        (selected[0], single),
    ]):
        transport = Transport([response("submit")])
        report = run_record(
            batch_package, tmp_path / f"record-{number}", task_id=task_id,
            completion=transport, options=RunOptions(preload=str(preload), network="none"),
        )
        assert report["status"] == "recorded" and report["verification"]["ok"], report
        assert report["task_id"] == task_id
        assert report["preload"]["preload_id"] == (
            "preload:sha256:" + open_preload(preload).metadata_sha256
        )
        assert report["cleanup_confirmed"] and len(transport.requests) == 1
    assert len(record_runtime.sandboxes) == 3
    assert all(sandbox.closed for sandbox in record_runtime.sandboxes)
