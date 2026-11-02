from types import SimpleNamespace
from uuid import uuid4

import pytest

from palinreplay.core.models import ArtifactRef, Node, Operation, Run, State, Step
from palinreplay.replay.initial import initial_recorder
from palinreplay.replay.services import PreparationError
from palinreplay.storage.btrfs import pool as pools
from palinreplay.storage.btrfs.models import BtrfsRuntime, PoolLocation, Snapshot
from palinreplay.storage.store import RecordStore
from tests.storage.test_btrfs_storage import C0LaunchConfig


@pytest.mark.parametrize("option", [None, "strict", "lenient", "prepared"])
def test_independent_c0_captures_actual_restored_workspace(tmp_path, monkeypatch, option):
    identity = uuid4()
    location = PoolLocation(volume="palinreplay-btrfs-" + identity.hex, uuid=identity,
                            daemon_id="fixture", helper_image_id="sha256:" + "a" * 64)
    source = Snapshot(location=location, name="checkpoints/" + uuid4().hex, uuid=uuid4())
    launch = C0LaunchConfig(platform="linux/amd64", environment={"PATH": "/bin"},
                           omitted_environment_keys=(), workdir="/", user="", shell=("bash", "-c"),
                           entrypoint=(), command=(), network_mode="none", memory_bytes=0, nano_cpus=0)
    runtime = BtrfsRuntime(source_container_id="old", source_image_id="source",
                           bootstrap_image_id="bootstrap", launch=launch, c0_oracle_sha256="oracle")
    calls = []
    pool = SimpleNamespace(
        location=location, verify=lambda s: None,
        snapshot=lambda work, name: calls.append((work, name)) or {"uuid": str(uuid4())},
        command=lambda op, **kw: calls.append((op, kw)) or {},
    )
    monkeypatch.setitem(pools._ACTIVE, (location.daemon_id, location.volume), pool)
    container = SimpleNamespace(id="new-container", attrs={"Config": {"Env": ["PATH=/bin"]}},
                                pause=lambda: calls.append("pause"),
                                unpause=lambda: calls.append("unpause"), reload=lambda: None)
    executor = SimpleNamespace(pool=pool, workspace="workspaces/" + uuid4().hex,
                               runtime=runtime, _container=container, check_root=lambda: None,
                               restored=SimpleNamespace(report={"snapshot": source.model_dump(mode="json")}))
    materials = SimpleNamespace(context={"messages": [{"role": "user", "content": "C20"}]},
                                input_position={"next_message": 1}, config={}, steps=(),
                                plan=SimpleNamespace(run=SimpleNamespace(task_id="t", agent_name="mini",
                                                                         agent_version="v"),
                                                     provenance=lambda: {"source_node": "C20"}))
    with RecordStore.create(tmp_path / "record") as store:
        preparation = {"status": "ready", "config": {"services": [{"name": "A"}]},
                       "services": [{"name": "A", "status": "ready", "startup": {"exit_code": 0}}]}
        options = ({"preparation": preparation} if option == "prepared" else
                   {} if option is None else {"validation_mode": option})
        rec = initial_recorder(store, materials, executor, **options)
        config = store.read_json(rec.run.config)["replay"]
        assert config["validation_mode"] == ("lenient" if option == "prepared" else option or "lenient")
        if option == "prepared":
            linked = config["preparation"]
            assert store.read_json(ArtifactRef.model_validate(linked["config"])) == preparation["config"]
            assert store.read_json(ArtifactRef.model_validate(linked["evidence"])) == preparation
        else:
            assert "preparation" not in config
        assert config["source"] == materials.plan.provenance()
        assert materials.config == {}
        assert store.list(Step) == store.list(Operation) == ()
        assert "continue_on_difference" not in config
        assert rec.run.source_node_id is None
        node = rec.current_node
        assert node.index == 0 and node.parent_node_id is None
        assert store.read_json(node.context) == materials.context
        state = store.get(State, node.state_id)
        assert state.logical_parent_id is None
        assert store.read_json(state.runtime)["source_container_id"] == "new-container"
        assert store.read_json(state.manifest)["name"] == "checkpoints/" + node.node_id
        assert calls[0] == "pause" and calls[2] == "unpause"
        assert not any(isinstance(c, tuple) and c[0] == "verify" for c in calls)
        assert len(store.list(Node)) == 1
        rec.finish()


@pytest.mark.parametrize("status", ["preparing", "failed"])
def test_preparation_must_succeed_before_c0_or_run_is_created(tmp_path, status):
    with RecordStore.create(tmp_path / "record") as store:
        with pytest.raises(PreparationError, match="must be ready"):
            initial_recorder(store, None, None, preparation={"status": status})
        assert store.list(Run) == store.list(Node) == ()
