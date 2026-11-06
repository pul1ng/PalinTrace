"""Exercise shared payloads through native recording and its public readers."""

import json
import shutil

import pytest

from palinreplay.agents import AgentTask
from palinreplay.core.interfaces import CallAdapter, Payload, ResultCapture
from palinreplay.core.models import Node, RunEnd
from palinreplay.core.replay_contract import source_reference
from palinreplay.preprocess.source import pin_source
from palinreplay.preprocess.timeline import read_timeline
from palinreplay.record.calls import RecordingError
from palinreplay.record.inspect import export_record, node_document, storage_metrics
from palinreplay.replay.config import replay_config
from palinreplay.replay.materials import load_replay_materials
from palinreplay.replay.plan import pin_replay
from palinreplay.storage.payloads import ROOT_MEDIA
from palinreplay.storage.store import RecordStore
from tests.agents.test_mini_agent import deterministic, integrated, response  # noqa: F401


def test_shared_native_history_export_and_replay_materials(tmp_path):
    store, sandbox, _, transport, agent = integrated(
        tmp_path, [response("long"), response("submit")],
        agent_config={"system_template": "history " * 2000},
    )
    try:
        agent.run(AgentTask("task", "Task"))
        nodes = store.list(Node)
        assert len(nodes) == 3
        assert all(n.context.media_type == ROOT_MEDIA for n in nodes)
        assert store.list(RunEnd)[0].details.media_type == ROOT_MEDIA
        assert len(transport.requests) == 2 and len(sandbox.calls) == 2
        assert len(store.read_json(nodes[0].context)["messages"]) == 2
        material = load_replay_materials(store, pin_replay(store, "C1", "C2"))
        config = replay_config(material)
        assert config["replay"]["source_start"]["logical_materials"]
        assert source_reference(config).start == nodes[1]
        timeline = read_timeline(store, pin_source(store))
        assert "head" in str(timeline)
        view = node_document(store, nodes[1])
        assert "head" in str(view["context_content"]["value"])
        assert storage_metrics(store)["referenced_artifact_bytes"] > 0
        assert store.check_integrity().ok
    finally:
        store.close()
    export_record(tmp_path / "record", tmp_path / "export", bundle=True)
    shutil.rmtree(tmp_path / "record")
    with RecordStore.open(tmp_path / "export/record") as reopened:
        assert reopened.check_integrity().ok
        assert reopened.read_json(nodes[1].context) == view["context_content"]["value"]
    data = json.loads((tmp_path / "export/trajectory.json").read_text())
    assert data["runs"]


def test_shared_request_mutation_stops_before_execution(tmp_path):
    from tests.record.test_source_reference import recorder

    executed = []
    request = {"messages": [{"role": "user", "content": "large " * 2000}]}
    with RecordStore.create(tmp_path / "record") as store:
        rec = recorder(store, {})
        rec.start()
        step = rec.begin_step()
        call = step.model(CallAdapter(
            name="fixture", execute=lambda req: executed.append(req),
            capture_request=Payload.json,
            capture_result=lambda result: ResultCapture(Payload.json(result), "fixture"),
        ), request)
        assert call.execution.request.media_type == ROOT_MEDIA
        request["messages"][0]["content"] = "changed"
        with pytest.raises(RecordingError):
            call.invoke()
        assert executed == []
        assert len(store.list(Node)) == 1
        rec.close()
