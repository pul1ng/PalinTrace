from copy import deepcopy
from types import SimpleNamespace

import pytest

from palinreplay.core.errors import IncompleteRecordError, IntegrityError
from palinreplay.core.interfaces import BoundaryAdapter, Payload
from palinreplay.core.models import Attempt, Node, Run, State, StateCapture, Step
from palinreplay.record.recorder import Recorder
from palinreplay.replay.config import replay_config
from palinreplay.replay.plan import pin_replay
from palinreplay.storage.store import ConflictError, RecordStore
from tests.helpers import recorder_smoke as fake
from tests.replay.test_plan import history


def configuration(tmp_path, start=20, end=22):
    with RecordStore.create(tmp_path / "source") as source:
        rec, _ = history(source, end)
        rec.finish()
        plan = pin_replay(source, f"C{start}", f"C{end}")
        return replay_config(SimpleNamespace(
            plan=plan, config=source.read_json(plan.run.config),
            context=source.read_json(plan.start.context),
            input_position=source.read_json(plan.start.input_position), steps=(),
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


@pytest.mark.parametrize("ordinary,index", [(True, 1), (False, 20), (False, 22)])
def test_parentless_step_requires_exact_declared_first_index(tmp_path, ordinary, index):
    config = {} if ordinary else configuration(tmp_path)
    with RecordStore.create(tmp_path / "actual") as store:
        rec = recorder(store, config)
        with pytest.raises(IntegrityError, match="parentless"):
            store.save(Step(run_id=rec.run.run_id, attempt_id=rec.attempt.attempt_id,
                            index=index, parent_node_id=None))
        rec.cancel()
        rec.close()
