import base64
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from palinreplay.core.errors import IntegrityError
from palinreplay.core.interfaces import AgentSnapshot, Payload
from palinreplay.core.models import Node, Run, RunEnd, State, Step
from palinreplay.record.source import source_dependency
from palinreplay.resume.recording import branch_config, open_recording
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake
from tests.record.test_source_reference import recorder
from tests.replay.test_plan import history


@pytest.fixture
def branch_session(tmp_path):
    path = tmp_path / "source"
    with RecordStore.create(path) as store:
        rec, _ = history(store, 20)
        rec.finish()
        node = rec.current_node
        materials = SimpleNamespace(run=rec.run, node=node, state=store.get(State, node.state_id),
                                    config=store.read_json(rec.run.config),
                                    runtime=SimpleNamespace(launch=SimpleNamespace(environment={})),
                                    physical_payloads={})
        for role, ref in (("config", rec.run.config), ("context", node.context),
                          ("input_position", node.input_position)):
            payload = store.read_payload(ref)
            setattr(materials, f"{role}_payload", payload)
            if ref.media_type != payload.media_type:
                materials.physical_payloads[role] = store.read_bytes(ref)
    output = tmp_path / "out"
    output.mkdir()
    attempt = {"status": "consuming", "complete_nodes": 0}
    return SimpleNamespace(
        start=SimpleNamespace(source_store=path, node=node, model="M", max_steps=1),
        materials=materials, executor=Mock(state="running"), output=output,
        report={"recording": attempt}, report_path=output / "report.json", _record_attempt=attempt,
    )


def test_open_branch_at_final_source_node_has_no_complete_local_node(branch_session):
    session = branch_session
    with open_recording(session, fake.FakeAgent().export) as (rec, step):
        store = rec.store
        assert step.step.index == 21 and step.step.parent_node_id is None
        assert store.list(Node) == ()
        assert len(store.list(Run)) == 1 and len(store.list(Step)) == 1
        assert rec.run.run_id != session.materials.run.run_id
        assert store.replay_source(rec.run.run_id) is None
        source = store.resume_source(rec.run.run_id)
        assert source.start == source.end == session.start.node
        assert source.source_store == str(session.start.source_store)
        assert source_dependency(store, rec.run.run_id, check=True)["mode"] == "resume"
        saved = json.loads(session.report_path.read_text())["recording"]
        assert saved["index"] == 21 and saved["complete_nodes"] == 0
    assert not session.executor.mock_calls
    with RecordStore.open(session.output / "record") as store:
        assert store.check_integrity().ok and not store.list(Node)


def test_source_queries_share_one_config_read_per_snapshot(branch_session, monkeypatch):
    with open_recording(branch_session, fake.FakeAgent().export) as (rec, _step):
        reads = []
        original = RecordStore.read_payload
        def read(store, ref):
            if store.root == rec.store.root and ref == rec.run.config:
                reads.append(ref)
            return original(store, ref)
        monkeypatch.setattr(RecordStore, "read_payload", read)
        for count in (1, 2):
            with rec.store.snapshot():
                source = rec.store.recording_source(rec.run.run_id)
                assert source.mode == "resume"
                assert rec.store.resume_source(rec.run.run_id) == source
                assert rec.store.replay_source(rec.run.run_id) is None
                assert rec.store.check_integrity().ok
                assert reads == [rec.run.config] * count
            assert rec.store._source_cache is None


def test_resume_has_no_historical_endpoint_and_keeps_local_parent_chain(branch_session):
    agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
    with RecordStore.create(branch_session.output / "record") as store:
        rec = recorder(store, branch_config(branch_session), agent, backend)
        rec.start_from_source()
        fake.answer(rec, agent, ())
        first = rec.commit_step()
        fake.answer(rec, agent, ())
        second = rec.commit_step()
        assert [n.index for n in store.list(Node)] == [21, 22]
        assert first.parent_node_id is None and second.parent_node_id == first.node_id
        assert store.get(State, first.state_id).logical_parent_id is None
        assert not store.list(RunEnd)
        rec.pause("controlled two-step budget")
        rec.close()
        end, = store.list(RunEnd)
        assert end.status == "paused" and end.last_node_id == second.node_id
        assert store.check_integrity().ok


@pytest.mark.parametrize("change", ["digest", "raw", "node", "mode", "conflict"])
def test_invalid_resume_source_cannot_open_a_branch(branch_session, change):
    config = branch_config(branch_session)
    resume = config["resume"]
    if change == "digest":
        resume["source_sha256"] = "0" * 64
    elif change == "raw":
        resume["raw_materials"]["context"] = base64.b64encode(b"{}").decode()
    elif change == "node":
        resume["source"]["node"]["state_id"] = "different"
    elif change == "mode":
        resume["initial_state_mode"] = "other"
    else:
        config["replay"] = {"old": "permission"}
    with RecordStore.create(branch_session.output / "record") as store:
        rec = recorder(store, config)
        with pytest.raises(IntegrityError):
            rec.start_from_source()
        assert not store.list(Node)
        rec.cancel()
        rec.close()


def test_parent_permissions_remain_only_in_pinned_source_material(branch_session):
    branch_session.materials.config.update(replay={"old": True}, resume={"old": True})
    config = branch_config(branch_session)
    assert "replay" not in config
    assert config["resume"]["source"]["node"]["node_id"] == branch_session.start.node.node_id
    assert config["resume"]["schema_version"] == "palinreplay.resume.v1"


@pytest.mark.parametrize("encoding", ["indented", "compressed"])
def test_resume_preserves_source_material_encoding(tmp_path, monkeypatch, encoding):
    from palinreplay.storage.codec import compress

    original = fake.FakeAgent.export
    def export(agent, boundary):
        snapshot = original(agent, boundary)
        def encoded(payload):
            value = json.loads(payload.data)
            value["padding"] = "x" * 5000
            data = json.dumps(value, indent=2).encode()
            if encoding == "compressed":
                data, media = compress(data)
                assert media != "application/json"
                return Payload(data, media)
            return Payload(data, "application/json")
        return AgentSnapshot(encoded(snapshot.context), encoded(snapshot.input_position))
    monkeypatch.setattr(fake.FakeAgent, "export", export)
    session = branch_session.__wrapped__(tmp_path)
    config = branch_config(session)
    with RecordStore.create(session.output / "record") as store:
        rec = recorder(store, deepcopy(config))
        rec.start_from_source()
        assert store.resume_source(rec.run.run_id).start == session.start.node
        rec.cancel()
        rec.close()
