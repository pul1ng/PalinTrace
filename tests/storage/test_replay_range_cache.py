from contextlib import nullcontext

import pytest

from palinreplay.core.errors import IntegrityError
from palinreplay.core.models import Run, Step
from palinreplay.record.inspect import export_record, inspect_record
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake
from tests.record.test_source_reference import configuration, recorder
from tests.storage.test_store import artifact_path


@pytest.fixture
def source_config(tmp_path):
    return configuration(tmp_path, start=1, end=4)


@pytest.fixture
def replay_record(tmp_path, source_config):
    with RecordStore.create(tmp_path / "actual") as store:
        agent = fake.FakeAgent()
        rec = recorder(store, source_config, agent)
        nodes = []
        try:
            rec.start_from_source()
            for _ in range(3):
                fake.answer(rec, agent, ())
                nodes.append(rec.commit_step())
            rec.finish()
        finally:
            rec.close()
        return store.root, rec.run, tuple(nodes)


def save_run(store, config_ref):
    return store.save(Run(task_id="cache-fixture", agent_name="fixture", agent_version="1",
                          config=config_ref))


def config_reads(monkeypatch, root, *refs):
    """Count only these configs in this store, including failed read attempts."""
    reads = []
    original = RecordStore.read_bytes

    def read(store, ref):
        if store.root == root and ref in refs:
            reads.append(ref)
        return original(store, ref)

    monkeypatch.setattr(RecordStore, "read_bytes", read)
    return reads


def test_integrity_reads_config_once_per_audit(replay_record, monkeypatch):
    root, run, _ = replay_record
    reads = config_reads(monkeypatch, root, run.config)
    with RecordStore.open(root) as store:
        for count in (1, 2):
            report = store.check_integrity()
            assert report.ok, report.issues
            assert not report.incomplete_steps
            assert reads == [run.config] * count
            assert store._source_cache is None


@pytest.mark.parametrize("in_transaction", [False, True])
def test_nested_snapshots_share_cache_and_leave_transaction_ownership(
    replay_record, monkeypatch, in_transaction,
):
    root, run, nodes = replay_record
    reads = config_reads(monkeypatch, root, run.config)
    with RecordStore.open(root, read_only=False) as store:
        transaction = store._transaction() if in_transaction else nullcontext()
        with transaction:
            with store.snapshot():
                source = store.replay_source(run.run_id)
                assert (source.start.index, source.end.index) == (1, 4)
                for node in nodes:
                    with store.snapshot():
                        assert store.get_node(node.node_id) == node
                        assert store.replay_source(run.run_id) == source
                        assert store.check_integrity().ok
                    assert reads == [run.config]
                assert store.replay_source(run.run_id) == source
                assert reads == [run.config]
            assert store._source_cache is None
            assert store._db.in_transaction is in_transaction
            # A caller-owned SQL transaction must not keep the cache alive.
            assert store.replay_source(run.run_id) == source
            assert reads == [run.config] * 2
            assert store._source_cache is None
        assert not store._db.in_transaction


@pytest.mark.parametrize(("data", "media_type", "reads_per_scope"), [
    (b'{"model":"fixture"}', "application/json", 1),
    (b"null", "application/json", 1),
    (b"{", "application/json", 1),
    (b"\xff", "application/json", 1),
    (b"opaque config", "text/plain", 0),
    (b"\x00\xff", "application/octet-stream", 0),
])
def test_ordinary_configs_cache_none_only_within_scope(
    tmp_path, monkeypatch, data, media_type, reads_per_scope,
):
    with RecordStore.create(tmp_path / "record") as store:
        run = save_run(store, store.put_bytes(data, media_type=media_type))
        reads = config_reads(monkeypatch, store.root, run.config)
        for count in (1, 2):
            with store.snapshot():
                for _ in range(3):
                    assert store.replay_source(run.run_id) is None
                # Non-JSON configs skip byte reads, but still need a cached None.
                assert list(store._source_cache.values()) == [None]
                assert reads == [run.config] * (reads_per_scope * count)
            assert store._source_cache is None


def test_multiple_runs_keep_separate_range_and_none_entries(tmp_path, source_config, monkeypatch):
    other_config = configuration(tmp_path / "other", start=2, end=3)
    with RecordStore.create(tmp_path / "actual") as store:
        first = save_run(store, store.put_json(source_config))
        ordinary = save_run(store, store.put_json({"ordinary": True}))
        second = save_run(store, store.put_json(other_config))
        refs = (first.config, ordinary.config, second.config)
        reads = config_reads(monkeypatch, store.root, *refs)
        with store.snapshot():
            for _ in range(3):
                a = store.replay_source(first.run_id)
                assert store.replay_source(ordinary.run_id) is None
                b = store.replay_source(second.run_id)
                assert (a.start.index, a.end.index) == (1, 4)
                assert (b.start.index, b.end.index) == (2, 3)
                assert a.run.run_id != b.run.run_id
            assert store.check_integrity().ok
            assert reads == list(refs)
        assert store._source_cache is None


@pytest.mark.parametrize("in_transaction", [False, True])
def test_scope_errors_preserve_outer_cache_then_clear_owned_cache(
    replay_record, monkeypatch, in_transaction,
):
    root, run, _ = replay_record
    reads = config_reads(monkeypatch, root, run.config)
    with RecordStore.open(root, read_only=False) as store:
        transaction = store._transaction() if in_transaction else nullcontext()
        with transaction:
            with pytest.raises(RuntimeError, match="outer scope"):
                with store.snapshot():
                    source = store.replay_source(run.run_id)
                    with pytest.raises(RuntimeError, match="nested scope"):
                        with store.snapshot():
                            assert store.replay_source(run.run_id) == source
                            raise RuntimeError("nested scope")
                    assert store.replay_source(run.run_id) == source
                    assert reads == [run.config]
                    raise RuntimeError("outer scope")
            assert store._source_cache is None
            assert store._db.in_transaction is in_transaction
            assert store.replay_source(run.run_id) == source
            assert reads == [run.config] * 2
            assert store._source_cache is None
        assert not store._db.in_transaction


def test_invalid_source_is_not_cached_as_none_and_error_scope_is_cleared(
    tmp_path, source_config, monkeypatch,
):
    source_config["replay"]["source_end_index"] += 1
    with RecordStore.create(tmp_path / "actual") as store:
        run = save_run(store, store.put_json(source_config))
        reads = config_reads(monkeypatch, store.root, run.config)
        with pytest.raises(IntegrityError, match="source range differs"):
            with store.snapshot():
                with pytest.raises(IntegrityError, match="source range differs"):
                    store.replay_source(run.run_id)
                assert store._source_cache == {}
                store.replay_source(run.run_id)
        assert store._source_cache is None
        assert not store._db.in_transaction
        with pytest.raises(IntegrityError, match="source range differs"):
            store.replay_source(run.run_id)
        assert reads == [run.config] * 3
        assert store._source_cache is None


@pytest.mark.parametrize("reader", ["source", "node", "audit"])
def test_later_reads_validate_corrupted_config_after_cache_scope_ends(
    replay_record, monkeypatch, reader,
):
    root, run, nodes = replay_record
    reads = config_reads(monkeypatch, root, run.config)
    with RecordStore.open(root) as store:
        with store.snapshot():
            assert store.replay_source(run.run_id) is not None
            assert store.check_integrity().ok
            assert reads == [run.config]
        path = artifact_path(store, run.config)
        path.chmod(0o600)
        path.write_bytes(b"\x00" * run.config.size)
        path.chmod(0o400)
        if reader == "audit":
            report = store.check_integrity()
            assert not report.ok
            assert any("size/hash mismatch" in issue.message for issue in report.issues)
        else:
            with pytest.raises(IntegrityError, match="size/hash mismatch"):
                if reader == "source":
                    store.replay_source(run.run_id)
                else:
                    store.get_node(nodes[-1].node_id)
        assert len(reads) > 1
        assert store._source_cache is None


def test_warm_range_cache_still_rejects_out_of_range_step(replay_record, monkeypatch):
    root, run, nodes = replay_record
    reads = config_reads(monkeypatch, root, run.config)
    with RecordStore.open(root, read_only=False) as store:
        tip = nodes[-1]
        illegal = Step(run_id=run.run_id, attempt_id=tip.attempt_id, index=tip.index + 1,
                       parent_node_id=tip.node_id)
        # Model an older writer, as in the source-reference endpoint tests.
        store._insert(illegal)
        with store.snapshot():
            assert store.replay_source(run.run_id).end.index == tip.index
            report = store.check_integrity()
            assert not report.ok
            assert any(issue.record == f"step/{illegal.step_id}"
                       and "pinned Replay range" in issue.message for issue in report.issues)
            assert reads == [run.config]


def test_inspection_and_export_bound_actual_config_reads(replay_record, tmp_path, monkeypatch):
    root, run, _ = replay_record
    reads = config_reads(monkeypatch, root, run.config)
    inspected = inspect_record(root, check=True)
    assert inspected["verification"]["ok"]
    assert len(inspected["runs"]) == 1
    assert reads == [run.config]
    exported = export_record(root, tmp_path / "export")
    assert exported["source_integrity_ok"]
    # Export has one fresh cached parse plus one read to include the config payload.
    assert reads == [run.config] * 3
