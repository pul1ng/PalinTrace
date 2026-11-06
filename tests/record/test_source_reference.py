import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from palinreplay.core.errors import IncompleteRecordError, IntegrityError
from palinreplay.core.interfaces import AgentSnapshot, BoundaryAdapter, Payload
from palinreplay.core.models import Attempt, Node, Run, RunEnd, State, StateCapture, Step
from palinreplay.record.recorder import Recorder
from palinreplay.replay.config import replay_config
from palinreplay.replay.plan import pin_replay
from palinreplay.storage.store import ConflictError, RecordStore
from tests.helpers import recorder_smoke as fake
from tests.replay.test_plan import history


def configuration(tmp_path, start=20, end=22, *, payload_layout="shared"):
    with RecordStore.create(tmp_path / "source", payload_layout=payload_layout) as source:
        rec, _ = history(source, end)
        rec.finish()
        plan = pin_replay(source, f"C{start}", f"C{end}")
        return replay_config(SimpleNamespace(
            plan=plan, config=source.read_json(plan.run.config),
            context=source.read_json(plan.start.context),
            input_position=source.read_json(plan.start.input_position), steps=(),
            context_bytes=source.read_bytes(plan.start.context),
            input_position_bytes=source.read_bytes(plan.start.input_position),
        ))


def recorder(store, config, agent=None, backend=None):
    agent, backend = agent or fake.FakeAgent(), backend or fake.FakeStateBackend()
    return Recorder(store, task_id="fixture", agent_name="fixture", agent_version="1",
                    config=Payload.json(config), boundary=BoundaryAdapter(agent.export, backend.capture))


def test_source_initialization_creates_only_run_attempt_and_config(tmp_path):
    config = configuration(tmp_path)
    backend = fake.FakeStateBackend()
    with RecordStore.create(tmp_path / "actual") as store:
        rec = recorder(store, config, backend=backend)
        rec.start_from_source()
        assert rec.phase == "ready" and rec.current_node is None
        assert len(store.list(Run)) == len(store.list(Attempt)) == 1
        assert store.list(Node) == store.list(State) == store.list(StateCapture) == ()
        assert backend.captures == []
        assert store.replay_source(rec.run.run_id).start.index == 20
        assert rec.run.source_node_id is None
        rec.cancel()
        rec.close()
        assert store.check_integrity().ok


@pytest.mark.parametrize("ending", ["finish", "pause"])
def test_empty_selected_range_ends_without_local_boundary(tmp_path, ending):
    config = configuration(tmp_path, end=20)
    backend = fake.FakeStateBackend()
    with RecordStore.create(tmp_path / "actual") as store:
        rec = recorder(store, config, backend=backend)
        rec.start_from_source()
        end = getattr(rec, ending)()
        rec.close()
        assert end.last_node_id is None
        assert end.status == ("completed" if ending == "finish" else "paused")
        assert not backend.captures
        assert store.list(Node) == store.list(State) == store.list(StateCapture) == store.list(Step) == ()
        assert store.check_integrity().ok


@pytest.mark.parametrize("case", ["ordinary", "nonempty-range", "unfinished-step"])
def test_missing_boundary_cannot_masquerade_as_empty_completion(tmp_path, case):
    config = {} if case == "ordinary" else configuration(tmp_path)
    with RecordStore.create(tmp_path / "actual") as store:
        rec = recorder(store, config)
        if case != "ordinary":
            rec.start_from_source()
        if case == "unfinished-step":
            rec.begin_step()
        with pytest.raises(IncompleteRecordError, match="complete boundary"):
            store.save(RunEnd(run_id=rec.run.run_id, attempt_id=rec.attempt.attempt_id,
                              status="completed", last_node_id=None,
                              details=store.put_json({})))
        rec.cancel()
        rec.close()
        assert store.check_integrity().ok


@pytest.mark.parametrize("change", ["mode", "digest", "range", "prefix", "ordinary"])
def test_source_initialization_requires_the_full_valid_contract(tmp_path, change):
    config = deepcopy(configuration(tmp_path))
    replay = config["replay"]
    if change == "mode":
        replay["node_numbering"] = "other"
    elif change == "digest":
        replay["source"]["selection_sha256"] = "0" * 64
    elif change == "range":
        replay["source_start_index"] = 19
    elif change == "prefix":
        replay["source_start"]["context"] = {"messages": ["future"]}
    else:
        config = {"ordinary": True}
    with RecordStore.create(tmp_path / "actual") as store:
        rec = recorder(store, config)
        with pytest.raises((IntegrityError, IncompleteRecordError)):
            rec.start_from_source()
        assert store.list(Node) == store.list(State) == ()
        rec.cancel()
        rec.close()


def test_actual_steps_keep_source_indices_and_local_parent_chain(tmp_path):
    config = configuration(tmp_path)
    agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
    backend.files["/prepared"] = {"hex": "41", "mode": 0o644}
    with RecordStore.create(tmp_path / "actual") as store:
        rec = recorder(store, config, agent, backend)
        with pytest.raises(IncompleteRecordError, match="start_from_source"):
            rec.start()
        assert backend.captures == []
        rec.start_from_source()
        fake.answer(rec, agent, ())
        first = rec.commit_step()
        backend.files["/second"] = {"hex": "42", "mode": 0o644}
        fake.answer(rec, agent, ())
        second = rec.commit_step()
        assert [n.index for n in store.list(Node)] == [21, 22]
        assert first.parent_node_id is None and second.parent_node_id == first.node_id
        assert first.node_id not in {n["node_id"] for n in config["replay"]["source"]["nodes"]}
        assert store.get_node(first.node_id) == first
        assert store.get_node(second.node_id) == second
        first_state = store.get(State, first.state_id)
        assert first_state.logical_parent_id is None and not first_state.dependencies
        assert "/prepared" in store.read_json(first_state.artifacts[0])
        assert [b.index for b in backend.captures] == [21, 22]
        assert store.read_json(first.context) != store.read_json(second.context)
        assert store.check_integrity().ok
        with pytest.raises(ConflictError, match="parent"):
            store.save(Step(run_id=rec.run.run_id, attempt_id=rec.attempt.attempt_id,
                            index=21, parent_node_id=None))
        rec.finish()
        rec.close()


@pytest.mark.parametrize("encoding", ["indented", "compressed"])
def test_source_prefix_validates_original_encoding_and_decoded_value(tmp_path, monkeypatch, encoding):
    from palinreplay.storage.codec import JSON_MEDIA, ZLIB_MEDIA, compress

    export = fake.FakeAgent.export
    def reencoded(agent, boundary):
        snapshot = export(agent, boundary)
        def encode(payload):
            value = json.loads(payload.data)
            value["padding"] = "x" * 5000
            data = json.dumps(value, indent=2).encode()
            if encoding == "compressed":
                data, media = compress(data)
                assert media == ZLIB_MEDIA
                return Payload(data, media)
            return Payload(data, JSON_MEDIA)
        return AgentSnapshot(encode(snapshot.context), encode(snapshot.input_position))
    monkeypatch.setattr(fake.FakeAgent, "export", reencoded)
    config = configuration(tmp_path, end=21)
    with RecordStore.open(tmp_path / "source") as source:
        assert source.check_integrity().ok
    agent = fake.FakeAgent()
    with RecordStore.create(tmp_path / "actual") as store:
        rec = recorder(store, config, agent)
        rec.start_from_source()
        fake.answer(rec, agent, ())
        assert rec.commit_step().index == 21
        rec.finish()
        rec.close()
        assert store.check_integrity().ok


@pytest.mark.parametrize("field", ["context", "input_position"])
def test_changed_raw_prefix_cannot_authorize_source_initialization(tmp_path, field):
    config = configuration(tmp_path)
    config["replay"]["source_start"]["raw_materials"][field] = "e30="
    with RecordStore.create(tmp_path / "actual") as store:
        rec = recorder(store, config)
        with pytest.raises(IntegrityError, match="protected material reference"):
            rec.start_from_source()
        rec.cancel()
        rec.close()


def test_earlier_canonical_v2_prefix_remains_readable(tmp_path):
    config = configuration(tmp_path, end=20, payload_layout="flat")
    del config["replay"]["source_start"]["raw_materials"]
    with RecordStore.create(tmp_path / "actual") as store:
        rec = recorder(store, config)
        rec.start_from_source()
        rec.finish()
        rec.close()
        assert store.check_integrity().ok


@pytest.mark.parametrize("end", [20, 21])
def test_recorder_rejects_any_step_after_pinned_endpoint(tmp_path, end):
    from palinreplay.record.calls import RecordingError

    config = configuration(tmp_path, end=end)
    agent = fake.FakeAgent()
    with RecordStore.create(tmp_path / "actual") as store:
        rec = recorder(store, config, agent)
        rec.start_from_source()
        if end == 21:
            fake.answer(rec, agent, ())
            rec.commit_step()
        with pytest.raises(RecordingError) as caught:
            rec.begin_step()
        assert "pinned Replay range" in str(caught.value.__cause__)
        assert [n.index for n in store.list(Node)] == ([] if end == 20 else [21])
        assert len(store.list(Step)) == end - 20
        rec.close()
        assert store.check_integrity().ok


@pytest.mark.parametrize("end", [20, 21])
def test_integrity_rejects_out_of_range_step_even_with_valid_parent_chain(tmp_path, end):
    config = configuration(tmp_path, end=end)
    agent = fake.FakeAgent()
    with RecordStore.create(tmp_path / "actual") as store:
        rec = recorder(store, config, agent)
        rec.start_from_source()
        if end == 21:
            fake.answer(rec, agent, ())
            rec.commit_step()
        illegal = Step(run_id=rec.run.run_id, attempt_id=rec.attempt.attempt_id, index=end + 1,
                       parent_node_id=rec.current_node.node_id if rec.current_node else None)
        # Simulate an older writer that omitted the endpoint guard.
        store._insert(illegal)
        checked = store.check_integrity()
        assert not checked.ok and "pinned Replay range" in str(checked.issues)
        rec.cancel()
        rec.close()


@pytest.mark.parametrize("ordinary,index", [(True, 1), (False, 20), (False, 22)])
def test_parentless_step_requires_exact_declared_first_index(tmp_path, ordinary, index):
    config = {} if ordinary else configuration(tmp_path)
    with RecordStore.create(tmp_path / "actual") as store:
        rec = recorder(store, config)
        with pytest.raises(IntegrityError, match=r"parentless|pinned Replay range"):
            store.save(Step(run_id=rec.run.run_id, attempt_id=rec.attempt.attempt_id,
                            index=index, parent_node_id=None))
        rec.cancel()
        rec.close()
