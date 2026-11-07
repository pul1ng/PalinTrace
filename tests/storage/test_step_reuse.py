from collections import Counter

import pytest

from palinreplay.core.models import Event, Execution, Node, Operation, ToolGroup
from palinreplay.storage.store import IncompleteRecordError, IntegrityError, RecordStore
from tests.helpers import recorder_smoke as fake
from tests.storage.test_store import artifact_path


def prepared(store, *, complete=True):
    agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
    recorder = fake.recorder(store, agent, backend)
    recorder.start()
    step = fake.answer(recorder, agent, ("first", "second"))
    tools = []
    for tool_id in ("first", "second") if complete else ("first",):
        call = step.tool(fake.tool_adapter(backend), {"action": "read"}, tool_call_id=tool_id)
        reply = call.invoke()
        fake.adopt(agent, call, {"role": "tool", "tool_call_id": tool_id, **reply})
        tools.append(call)
    return recorder, step, tools


def test_step_check_reads_each_fact_collection_once(tmp_path, monkeypatch):
    with RecordStore.create(tmp_path / "record") as store:
        recorder, step, tools = prepared(store)
        counts = Counter()
        original = store.list

        def listed(model, **kwargs):
            counts[model] += 1
            return original(model, **kwargs)

        monkeypatch.setattr(store, "list", listed)
        calls = store.accepted_step(step.step.step_id)
        assert calls.tool_execution_ids == tuple(call.execution_id for call in tools)
        assert calls.accepted_event_sequence == 12
        assert {model: counts[model] for model in (Event, Execution, Operation, ToolGroup)} == {
            Event: 1, Execution: 1, Operation: 1, ToolGroup: 1,
        }
        node = recorder.commit_step()
        assert node.tool_execution_ids == calls.tool_execution_ids
        recorder.finish()
        recorder.close()
        assert store.check_integrity().ok


def test_step_fact_reuse_keeps_missing_tool_check(tmp_path):
    with RecordStore.create(tmp_path / "record") as store:
        recorder, step, _ = prepared(store, complete=False)
        with pytest.raises(IncompleteRecordError, match="missing or extra"):
            store.accepted_step(step.step.step_id)
        assert len(store.list(Node)) == 1
        recorder.cancel()
        recorder.close()


def test_step_fact_reuse_still_verifies_result_materials(tmp_path):
    with RecordStore.create(tmp_path / "record") as store:
        recorder, step, tools = prepared(store)
        result = store.execution_events(tools[-1].execution_id)[1]
        path = artifact_path(store, result.payload)
        path.chmod(0o600)
        path.write_bytes(b"broken result")
        with pytest.raises(IntegrityError):
            store.accepted_step(step.step.step_id)
        assert len(store.list(Node)) == 1
        recorder.cancel()
        recorder.close()
