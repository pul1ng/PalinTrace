from collections import Counter
from types import SimpleNamespace
from uuid import uuid4

import pytest

import palinreplay.record.inspect as record_inspect
import palinreplay.storage.btrfs.pool as btrfs_pool
import palinreplay.storage.btrfs.verification as verification
from palinreplay.core.errors import IntegrityError
from palinreplay.core.interfaces import BoundaryAdapter, Payload
from palinreplay.core.models import State
from palinreplay.record.recorder import Recorder
from palinreplay.record.source import source_dependency
from palinreplay.replay.config import replay_config
from palinreplay.replay.plan import pin_replay
from palinreplay.storage.btrfs.models import BACKEND, PoolLocation, Snapshot
from palinreplay.storage.io import write_json
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake


def location():
    identity = uuid4()
    return PoolLocation(
        volume="palinreplay-btrfs-" + identity.hex,
        uuid=identity,
        daemon_id="fixture-daemon",
        helper_image_id="sha256:" + "a" * 64,
    )


def record(root, pool_location, *, config=None):
    snapshots = []
    agent = fake.FakeAgent()

    def capture(store, boundary, previous):
        snapshot = Snapshot(
            location=pool_location,
            name="checkpoints/" + boundary.node_id,
            uuid=uuid4(),
        )
        snapshots.append(snapshot)
        manifest = store.put_json(snapshot.model_dump(mode="json"))
        return State(
            backend=BACKEND,
            capture=verification.capture_info(),
            manifest=manifest,
            runtime=store.put_json({}),
            artifacts=(manifest,),
        )

    with RecordStore.create(root) as store:
        write_json(root / "btrfs.json", pool_location.model_dump(mode="json"))
        rec = Recorder(
            store,
            task_id="fixture",
            agent_name="fixture",
            agent_version="1",
            config=Payload.json(config or {}),
            boundary=BoundaryAdapter(agent.export, capture),
        )
        if config is None:
            rec.start()
        else:
            rec.start_from_source()
        for _ in range(2):
            fake.answer(rec, agent, ())
            rec.commit_step()
        rec.finish()
        rec.close()
        run_id = rec.run.run_id
    return snapshots, run_id


@pytest.fixture(params=["local", "replay-shared-pool", "replay-separate-pools"])
def history(tmp_path, monkeypatch, request):
    actual_location = location()
    source_location = location() if request.param == "replay-separate-pools" else actual_location
    root, source = tmp_path / "actual", tmp_path / "source"
    source_snapshots, config = [], None
    # Recording has an existing pool owner. Only invocation sessions below are measured.
    with monkeypatch.context() as setup:
        for selected in (actual_location, source_location):
            setup.setitem(
                btrfs_pool._ACTIVE,
                (selected.daemon_id, selected.volume),
                SimpleNamespace(verify=lambda snapshot: None),
            )
        if request.param != "local":
            source_snapshots, _ = record(source, source_location)
            with RecordStore.open(source) as origin:
                plan = pin_replay(origin, "C0", "C2")
                config = replay_config(SimpleNamespace(
                    plan=plan,
                    config=origin.read_json(plan.run.config),
                    context=origin.read_json(plan.start.context),
                    input_position=origin.read_json(plan.start.input_position),
                    context_bytes=origin.read_bytes(plan.start.context),
                    input_position_bytes=origin.read_bytes(plan.start.input_position),
                    steps=(),
                ))
        actual_snapshots, run_id = record(root, actual_location, config=config)
    return SimpleNamespace(
        root=root,
        run_id=run_id,
        actual=actual_snapshots,
        source=source_snapshots,
        snapshots=actual_snapshots + source_snapshots,
    )


@pytest.fixture
def sessions(monkeypatch):
    state = SimpleNamespace(clients=[], pools=[], verified=[], fail=None)

    class Client:
        closed = False

        def close(self):
            assert not self.closed
            assert all(pool.closed for pool in state.pools if pool.client is self)
            self.closed = True

    def connect(timeout):
        assert timeout == 600
        client = Client()
        state.clients.append(client)
        return client

    class Pool:
        def __init__(self, client, pool_location):
            self.client, self.location = client, pool_location
            self.closed = False
            self.key = (pool_location.daemon_id, pool_location.volume)
            state.pools.append(self)

        def __enter__(self):
            assert self.key not in btrfs_pool._ACTIVE
            btrfs_pool._ACTIVE[self.key] = self
            return self

        def __exit__(self, *exc):
            assert not self.closed and not self.client.closed
            assert btrfs_pool._ACTIVE.pop(self.key) is self
            self.closed = True

        def verify(self, snapshot):
            assert not self.closed and not self.client.closed
            assert snapshot.location == self.location
            state.verified.append(snapshot)
            if snapshot == state.fail:
                raise FileNotFoundError("snapshot was deleted")

    monkeypatch.setattr(verification, "connect_docker", connect)
    monkeypatch.setattr(verification, "BtrfsPool", Pool)
    return state


def assert_closed(sessions):
    assert sessions.clients and len(sessions.clients) == len(sessions.pools)
    assert all(client.closed for client in sessions.clients)
    assert all(pool.closed and pool.key not in btrfs_pool._ACTIVE for pool in sessions.pools)
    assert verification._CHECKS.get() is None


def invoke(operation, root, output):
    if operation == "inspect":
        return record_inspect.inspect_record(root, check=True)
    return record_inspect.export_record(root, output)


@pytest.mark.parametrize("operation", ["inspect", "export"])
def test_invocation_reuses_each_pool_and_checks_every_snapshot(
    history, sessions, tmp_path, operation, monkeypatch,
):
    metrics = record_inspect.storage_metrics

    def while_building_document(store):
        # Sessions must survive both the local audit and source validation.
        active_pools = sessions.pools[session_start:]
        assert active_pools and all(not pool.closed for pool in active_pools)
        assert {snapshot.uuid for snapshot in sessions.verified} == {
            snapshot.uuid for snapshot in history.snapshots
        }
        return metrics(store)

    monkeypatch.setattr(record_inspect, "storage_metrics", while_building_document)
    expected = Counter({snapshot.location.volume: 1 for snapshot in history.snapshots})
    for attempt in range(2):
        session_start = len(sessions.pools)
        result = invoke(operation, history.root, tmp_path / f"export-{attempt}")
        if operation == "inspect":
            assert result["verification"]["ok"]
        else:
            assert result["source_integrity_ok"]
        assert len(result["external_dependencies"]) == bool(history.source)
        assert all(dependency["checked"] for dependency in result["external_dependencies"])
        assert Counter(pool.location.volume for pool in sessions.pools) == {
            volume: count * (attempt + 1) for volume, count in expected.items()
        }
        assert_closed(sessions)
        sessions.verified.clear()


def test_direct_source_check_shares_a_session_across_nodes(history, sessions):
    if not history.source:
        pytest.skip("requires an external source")
    with RecordStore.open(history.root) as store:
        dependency = source_dependency(store, history.run_id, check=True)
    assert dependency["checked"]
    assert {snapshot.uuid for snapshot in sessions.verified} == {
        snapshot.uuid for snapshot in history.source
    }
    assert len(sessions.pools) == 1
    assert_closed(sessions)


@pytest.mark.parametrize("operation", ["inspect", "export"])
@pytest.mark.parametrize("damaged", ["actual", "source"])
def test_snapshot_failure_closes_sessions_and_prevents_export(
    history, sessions, tmp_path, operation, damaged,
):
    snapshots = getattr(history, damaged)
    if not snapshots:
        pytest.skip("requires an external source")
    sessions.fail = snapshots[-1]
    output = tmp_path / "export"
    if operation == "inspect":
        result = invoke(operation, history.root, output)
        assert not result["verification"]["ok"]
        assert result["runs"] == []
        if damaged == "source":
            assert "required Replay source unavailable" in str(result["verification"]["issues"])
    else:
        with pytest.raises(IntegrityError):
            invoke(operation, history.root, output)
        assert not output.exists()
    assert sessions.fail in sessions.verified
    assert {snapshot.uuid for snapshot in snapshots} <= {
        snapshot.uuid for snapshot in sessions.verified
    }
    assert_closed(sessions)


@pytest.mark.parametrize("operation", ["inspect", "export"])
def test_document_error_closes_sessions(history, sessions, tmp_path, operation, monkeypatch):
    def fail_document(store):
        assert sessions.pools and all(not pool.closed for pool in sessions.pools)
        raise RuntimeError("injected document failure")

    monkeypatch.setattr(record_inspect, "storage_metrics", fail_document)
    output = tmp_path / "export"
    with pytest.raises(RuntimeError, match="injected document failure"):
        invoke(operation, history.root, output)
    assert not output.exists()
    assert_closed(sessions)


def test_unchecked_inspect_needs_no_pool_session(history, sessions):
    result = record_inspect.inspect_record(history.root)
    assert not result["verification"]["checked"]
    assert all(not dependency["checked"] for dependency in result["external_dependencies"])
    assert not sessions.clients and not sessions.pools and not sessions.verified
    assert verification._CHECKS.get() is None
