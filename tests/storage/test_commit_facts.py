from collections import Counter

import pytest

from palinreplay.core.models import Event, Execution, Node, Operation, ToolGroup
from palinreplay.storage.store import IncompleteRecordError, IntegrityError, RecordStore
from tests.storage.test_step_reuse import prepared
from tests.storage.test_store import artifact_path


def candidate(store, recorder, step):
    calls = store.accepted_step(step.step.step_id)
    return Node(
        run_id=recorder.run.run_id, attempt_id=recorder.attempt.attempt_id,
        index=step.step.index, step_id=step.step.step_id,
        parent_node_id=recorder.current_node.node_id,
        context=store.put_json({"messages": []}),
        input_position=store.put_json({"next_message": 0}),
        state_id=recorder.current_node.state_id, **calls.model_dump(),
    )


def test_final_commit_reads_each_fact_collection_once(tmp_path, monkeypatch):
    with RecordStore.create(tmp_path / "record") as store:
        recorder, step, tools = prepared(store)
        node = candidate(store, recorder, step)
        counts = Counter()
        original = store.list

        def listed(model, **kwargs):
            counts[model] += 1
            return original(model, **kwargs)

        monkeypatch.setattr(store, "list", listed)
        store.commit_node(node)
        assert {model: counts[model] for model in (Event, Operation, Execution, ToolGroup)} == {
            Event: 1, Operation: 1, Execution: 1, ToolGroup: 1,
        }
        assert node.tool_execution_ids == tuple(tool.execution_id for tool in tools)
        assert store.get_node(node.node_id) == node
        assert store.check_integrity().ok


def test_final_commit_and_existing_reader_verify_identical_material_refs(tmp_path, monkeypatch):
    with RecordStore.create(tmp_path / "record") as store:
        recorder, step, _ = prepared(store)
        node = candidate(store, recorder, step)
        refs = Counter()
        verify = store._verify_artifact

        def checked(ref):
            refs[(ref.sha256, ref.size, ref.media_type)] += 1
            return verify(ref)

        monkeypatch.setattr(store, "_verify_artifact", checked)
        with store._artifacts.strict_checks():
            store.commit_node(node)
        commit_refs = refs.copy()
        refs.clear()
        with store._artifacts.strict_checks(), store.snapshot():
            store._node_records(node)
        assert refs == commit_refs and refs


@pytest.mark.parametrize("damage", ["catalog", "payload", "missing-result", "missing-operation"])
def test_final_commit_reloads_facts_and_rejects_damage(tmp_path, damage):
    with RecordStore.create(tmp_path / "record") as store:
        recorder, step, tools = prepared(store)
        node = candidate(store, recorder, step)
        result = store.execution_events(tools[-1].execution_id)[1]
        if damage == "catalog":
            store._db.execute("UPDATE records SET sha256=? WHERE kind='event' AND id=?",
                              ("0" * 64, result.event_id))
        elif damage == "payload":
            path = artifact_path(store, result.payload)
            path.chmod(0o600)
            path.write_bytes(b"corrupt")
        elif damage == "missing-result":
            store._db.execute("DELETE FROM records WHERE kind='event' AND id=?",
                              (result.event_id,))
        else:
            store._db.execute("DELETE FROM records WHERE kind='operation' AND id=?",
                              (tools[-1].operation.operation_id,))
        with pytest.raises((IntegrityError, IncompleteRecordError)):
            store.commit_node(node)
        assert [n.index for n in store.list(Node)] == [0]


def test_final_commit_failure_rolls_back_and_next_commit_rechecks(tmp_path, monkeypatch):
    with RecordStore.create(tmp_path / "record") as store:
        recorder, step, _ = prepared(store)
        node = candidate(store, recorder, step)
        insert = store._insert

        def fail(record):
            insert(record)
            if isinstance(record, Node):
                raise OSError("fixture publication failure")

        monkeypatch.setattr(store, "_insert", fail)
        with pytest.raises(OSError):
            store.commit_node(node)
        assert [n.index for n in store.list(Node)] == [0]
        monkeypatch.setattr(store, "_insert", insert)
        assert store.commit_node(node) == node
        assert store.get_node(node.node_id) == node
