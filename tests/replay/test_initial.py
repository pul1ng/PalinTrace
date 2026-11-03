from types import SimpleNamespace

import pytest

from palinreplay.core.models import Node, Operation, Run, State, StateCapture, Step
from palinreplay.replay.initial import initial_recorder
from palinreplay.replay.materials import ReplayMaterials
from palinreplay.replay.plan import pin_replay
from palinreplay.replay.services import PreparationError
from palinreplay.storage.store import RecordStore
from tests.record.test_source_reference import configuration


@pytest.mark.parametrize("prepared", [False, True])
def test_initialization_references_c20_without_capture(tmp_path, prepared):
    configuration(tmp_path)
    with RecordStore.open(tmp_path / "source") as source:
        plan = pin_replay(source, "C20", "C22")
        materials = ReplayMaterials(plan, source.read_json(plan.run.config),
                                    source.read_json(plan.start.context),
                                    source.read_json(plan.start.input_position), (),
                                    source.read_bytes(plan.start.context),
                                    source.read_bytes(plan.start.input_position))
    # No pool/container/capture API is provided: initialization must never use one.
    executor = SimpleNamespace(runtime=SimpleNamespace(launch=SimpleNamespace(environment={})))
    preparation = {"status": "ready", "config": {"services": [{"name": "A"}]},
                   "services": [{"name": "A", "status": "ready"}]} if prepared else None
    with RecordStore.create(tmp_path / "record") as store:
        rec = initial_recorder(store, materials, executor, preparation=preparation)
        saved = store.read_json(rec.run.config)["replay"]
        assert saved["schema_version"] == "palinreplay.replay.v2"
        assert saved["source"] == plan.provenance()
        assert not saved["preparation_state_captured"]
        assert rec.current_node is None and rec.phase == "ready"
        assert store.list(Node) == store.list(State) == store.list(StateCapture) == ()
        assert store.list(Step) == store.list(Operation) == ()
        if prepared:
            assert saved["preparation"] == {"config": preparation["config"], "evidence": preparation}
        else:
            assert "preparation" not in saved
        assert rec.run.source_node_id is None
        rec.cancel()
        rec.close()


@pytest.mark.parametrize("status", ["preparing", "failed"])
def test_preparation_must_succeed_before_run_is_created(tmp_path, status):
    with RecordStore.create(tmp_path / "record") as store:
        with pytest.raises(PreparationError, match="must be ready"):
            initial_recorder(store, None, None, preparation={"status": status})
        assert store.list(Run) == store.list(Node) == ()
