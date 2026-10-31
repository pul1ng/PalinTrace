import os
import sqlite3
import subprocess
import sys

import pytest

from palinreplay.core.models import (
    ArtifactRef,
    Attempt,
    CaptureInfo,
    Event,
    Execution,
    Failure,
    Node,
    Operation,
    Run,
    State,
    Step,
)
from palinreplay.storage.store import (
    ConflictError,
    IncompleteRecordError,
    IntegrityError,
    RecordStore,
    StoreError,
)


def test_catalog_format_identifier_is_independent_of_python_module_path(tmp_path):
    path = tmp_path / "record"
    with RecordStore.create(path):
        pass
    with sqlite3.connect(path / "catalog.sqlite") as catalog:
        assert catalog.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("palinreplay.store.v1",)
    with RecordStore.open(path) as store:
        assert store.check_integrity().ok


def material(store, *, complete=True, dependencies=(), logical_parent_id=None):
    return store.save(
        State(
            backend="fake-files-v1",
            capture=CaptureInfo(
                scope="fixture",
                included=("/testbed", "/opt"),
                attributes=("content", "mode"),
                consistency="fixture",
                complete=complete,
                limitations=("synthetic material; no container restore",),
            ),
            manifest=store.put_json({"paths": ["/testbed/a", "/opt/config"]}),
            runtime=store.put_json({"network": "none", "mounts": []}),
            artifacts=(store.put_bytes(b"file data\x00\xff\r\n"),),
            dependencies=dependencies,
            logical_parent_id=logical_parent_id,
        )
    )


def start(store, *, name="run"):
    run = store.save(
        Run(
            run_id=name,
            task_id="synthetic",
            agent_name="fake",
            agent_version="fixed",
            config=store.put_json({"model": "fake", "credentials": "omitted"}),
        )
    )
    attempt = store.save(Attempt(run_id=run.run_id))
    state = material(store)
    node = store.commit_node(
        Node(
            run_id=run.run_id,
            attempt_id=attempt.attempt_id,
            index=0,
            context=store.put_json([]),
            input_position=store.put_json({"messages": 0}),
            state_id=state.state_id,
            accepted_event_sequence=0,
        )
    )
    return run, attempt, state, node


def begin_step(store, run, attempt, parent):
    return store.save(
        Step(
            run_id=run.run_id,
            attempt_id=attempt.attempt_id,
            index=parent.index + 1,
            parent_node_id=parent.node_id,
        )
    )


def call(store, step, *, kind="tool", outcome="returned", stages=4, operation=None, index=1):
    operation = operation or store.save(
        Operation(run_id=step.run_id, step_id=step.step_id, kind=kind, name="fake")
    )
    execution = store.save(
        Execution(
            run_id=step.run_id,
            operation_id=operation.operation_id,
            index=index,
            request=store.put_bytes(b"request\r\n"),
        )
    )
    if stages >= 1:
        store.append_event(execution.execution_id, "dispatched")
    if stages >= 2:
        store.append_event(
            execution.execution_id,
            "result",
            payload=store.put_bytes(b"raw\x00\xff\r\n"),
            details=store.put_json({"exit_code": 1 if outcome == "error" else 0}),
            outcome=outcome,
        )
    if stages >= 3:
        store.append_event(
            execution.execution_id, "delivered", payload=store.put_bytes(b"observation")
        )
    if stages >= 4:
        store.append_event(
            execution.execution_id, "accepted", payload=store.put_json({"content": "adopted"})
        )
    return execution


def candidate(store, step, state, model, tools=()):
    return Node(
        run_id=step.run_id,
        attempt_id=step.attempt_id,
        index=step.index,
        parent_node_id=step.parent_node_id,
        step_id=step.step_id,
        context=store.put_json({"messages": ["step", step.index]}),
        input_position=store.put_json({"messages": step.index + 1}),
        state_id=state.state_id,
        accepted_event_sequence=max(e.sequence for e in store.list(Event, run_id=step.run_id)),
        model_execution_id=model.execution_id,
        tool_operation_ids=tuple(e.operation_id for e in tools),
        tool_execution_ids=tuple(e.execution_id for e in tools),
    )


def artifact_path(store, ref):
    return store.root / "artifacts" / ref.sha256[:2] / ref.sha256[2:]


@pytest.fixture
def store(tmp_path):
    with RecordStore.create(tmp_path / "record") as value:
        yield value


def test_two_steps_reopen_shared_state_and_preserve_raw_vs_accepted_bytes(store):
    run, attempt, state, c0 = start(store)
    step1 = begin_step(store, run, attempt, c0)
    model1, tool1 = call(store, step1, kind="model"), call(store, step1)
    c1 = store.commit_node(candidate(store, step1, state, model1, (tool1,)))
    step2 = begin_step(store, run, attempt, c1)
    model2, tool2 = call(store, step2, kind="model"), call(store, step2, outcome="error")
    c2 = store.commit_node(candidate(store, step2, state, model2, (tool2,)))
    assert c1.state_id == c2.state_id and c1.node_id != c2.node_id
    assert c1.context != c2.context and tool1.execution_id != tool2.execution_id
    store.close()
    with RecordStore.open(store.root) as reopened:
        assert reopened.get_node(c1.node_id) == c1
        assert reopened.get_node(c2.node_id) == c2
        events = [e for e in reopened.list(Event) if e.execution_id == tool1.execution_id]
        assert reopened.read_bytes(events[1].payload) == b"raw\x00\xff\r\n"
        assert reopened.read_bytes(events[2].payload) == b"observation"
        assert reopened.read_json(events[3].payload) == {"content": "adopted"}
        report = reopened.check_integrity()
        assert report.ok and not report.incomplete_steps and not report.pending_executions
        assert len(reopened.list(Node)) == 3
        with pytest.raises(StoreError, match="read-only"):
            reopened.put_bytes(b"write")


def test_import_copies_bytes_deduplicates_and_survives_source_deletion(store, tmp_path):
    source = tmp_path / "capture.tar"
    source.write_bytes(b"opaque archive\x00\r\n")
    first = store.put_file(source)
    second = store.put_bytes(source.read_bytes())
    assert first == second
    assert source.stat().st_ino != artifact_path(store, first).stat().st_ino
    source.write_bytes(b"later contents")
    source.unlink()
    assert store.read_bytes(first) == b"opaque archive\x00\r\n"
    assert len(list((store.root / "artifacts" / first.sha256[:2]).iterdir())) == 1


def test_existing_record_is_idempotent_but_conflicting_content_is_rejected(store):
    run, _, _, c0 = start(store)
    assert store.save(run) == run
    assert store.commit_node(c0) == c0
    changed = run.model_copy(update={"agent_version": "changed"})
    with pytest.raises(ConflictError):
        store.save(changed)
    assert store.get(Run, run.run_id) == run


@pytest.mark.parametrize("stage", [0, 1, 2, 3])
def test_partial_tool_group_cannot_publish_node(store, stage):
    run, attempt, state, c0 = start(store)
    step = begin_step(store, run, attempt, c0)
    model = call(store, step, kind="model")
    tool = call(store, step, stages=stage)
    with pytest.raises(IncompleteRecordError):
        store.commit_node(candidate(store, step, state, model, (tool,)))
    assert store.list(Node) == (c0,)
    report = store.check_integrity()
    assert report.ok and report.incomplete_steps == (step.step_id,)
    assert (tool.execution_id in report.pending_executions) == (stage < 2)


def test_unknown_result_is_preserved_but_cannot_complete_node(store):
    run, attempt, state, c0 = start(store)
    step = begin_step(store, run, attempt, c0)
    model, tool = call(store, step, kind="model"), call(store, step, outcome="unknown")
    with pytest.raises(IncompleteRecordError, match="unknown"):
        store.commit_node(candidate(store, step, state, model, (tool,)))
    assert store.check_integrity().pending_executions == (tool.execution_id,)


def test_retries_are_distinct_and_only_selected_execution_is_accepted(store):
    run, attempt, state, c0 = start(store)
    step = begin_step(store, run, attempt, c0)
    failed = call(store, step, kind="model", outcome="error", stages=2)
    operation = store.get(Operation, failed.operation_id)
    selected = call(store, step, kind="model", operation=operation, index=2)
    node = store.commit_node(candidate(store, step, state, selected))
    assert store.get_node(node.node_id) == node
    assert len(store.list(Execution)) == 2


def test_event_order_and_persist_before_delivery(store):
    run, attempt, _, c0 = start(store)
    step = begin_step(store, run, attempt, c0)
    execution = call(store, step, stages=0)
    with pytest.raises(IncompleteRecordError):
        store.append_event(
            execution.execution_id, "result", payload=store.put_bytes(b"result"), outcome="returned"
        )
    store.append_event(execution.execution_id, "dispatched")
    raw = store.put_bytes(b"complete raw output")
    store.append_event(execution.execution_id, "result", payload=raw, outcome="returned")
    artifact_path(store, raw).unlink()
    with pytest.raises(IntegrityError):
        store.append_event(execution.execution_id, "delivered", payload=store.put_bytes(b"display"))
    assert [e.kind for e in store.list(Event)] == ["dispatched", "result"]


@pytest.mark.parametrize("damage", ["missing", "corrupt", "symlink", "wrong-size"])
def test_required_artifact_damage_prevents_publication_and_is_reported(store, damage):
    run, attempt, state, c0 = start(store)
    step = begin_step(store, run, attempt, c0)
    model = call(store, step, kind="model")
    node = candidate(store, step, state, model)
    ref = node.context
    path = artifact_path(store, ref)
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.chmod(0o600)
        path.write_bytes(b"x" * ref.size)
    elif damage == "symlink":
        path.unlink()
        path.symlink_to(artifact_path(store, c0.context))
    else:
        node = node.model_copy(
            update={"context": ArtifactRef(sha256=ref.sha256, size=ref.size + 1)}
        )
    with pytest.raises(IntegrityError):
        store.commit_node(node)
    assert store.list(Node) == (c0,)


def test_deleting_state_material_after_commit_invalidates_read_and_audit(store):
    _, _, state, c0 = start(store)
    artifact_path(store, state.artifacts[0]).unlink()
    with pytest.raises(IntegrityError):
        store.get_node(c0.node_id)
    report = store.check_integrity()
    assert not report.ok
    assert any(issue.record == f"node/{c0.node_id}" for issue in report.issues)


def test_incomplete_capture_and_missing_transitive_dependency_block_node(store):
    run, attempt, state, c0 = start(store)
    partial = material(store, complete=False)
    step = begin_step(store, run, attempt, c0)
    model = call(store, step, kind="model")
    with pytest.raises(IncompleteRecordError, match="incomplete capture"):
        store.commit_node(candidate(store, step, partial, model))
    base = store.save(
        state.model_copy(
            update={
                "state_id": "base-with-unique-content",
                "artifacts": (store.put_bytes(b"base-only-content"),),
            }
        )
    )
    derived = material(store, dependencies=(base.state_id,), logical_parent_id=partial.state_id)
    artifact_path(store, base.artifacts[0]).unlink()
    with pytest.raises(IntegrityError):
        store.commit_node(candidate(store, step, derived, model))
    assert store.list(Node) == (c0,)


def test_omitting_tool_operation_or_execution_is_not_a_complete_group(store):
    run, attempt, state, c0 = start(store)
    step = begin_step(store, run, attempt, c0)
    model = call(store, step, kind="model")
    tool = call(store, step)
    with pytest.raises(IncompleteRecordError, match="tool group"):
        store.commit_node(candidate(store, step, state, model))
    node = candidate(store, step, state, model, (tool,))
    with pytest.raises(IncompleteRecordError, match="tool group"):
        store.commit_node(node.model_copy(update={"tool_operation_ids": ("missing-call",)}))


def test_committed_history_is_frozen_and_later_steps_keep_older_nodes_readable(store):
    run, attempt, state, c0 = start(store)
    step = begin_step(store, run, attempt, c0)
    model = call(store, step, kind="model")
    node = store.commit_node(candidate(store, step, state, model))
    with pytest.raises(ConflictError, match="committed step"):
        call(store, step)
    with pytest.raises(ConflictError, match="committed step"):
        call(store, step, kind="model", operation=store.get(Operation, model.operation_id), index=2)
    with pytest.raises(ConflictError):
        store.commit_node(node.model_copy(update={"context": store.put_json(["changed"])}))
    with pytest.raises(ConflictError):
        begin_step(store, run, attempt, c0)
    assert store.get_node(c0.node_id) == c0 and store.get_node(node.node_id) == node


def test_cross_run_and_stale_event_boundary_are_rejected(store):
    run, attempt, state, c0 = start(store)
    _, other_attempt, _, other_c0 = start(store, name="other")
    with pytest.raises(IntegrityError, match="cross-run"):
        begin_step(store, run, attempt, other_c0)
    step = begin_step(store, run, attempt, c0)
    model = call(store, step, kind="model")
    node = candidate(store, step, state, model)
    with pytest.raises(IntegrityError):
        store.commit_node(node.model_copy(update={"attempt_id": other_attempt.attempt_id}))
    with pytest.raises(IntegrityError, match="input boundary"):
        store.commit_node(node.model_copy(update={"accepted_event_sequence": 1}))


def test_insert_failure_rolls_back_publication_and_retains_unfinished_facts(store):
    run, attempt, state, c0 = start(store)
    step = begin_step(store, run, attempt, c0)
    model = call(store, step, kind="model")
    node = candidate(store, step, state, model)
    store._db.execute("""CREATE TRIGGER fail_node AFTER INSERT ON records WHEN NEW.kind='node'
                         BEGIN SELECT RAISE(ABORT, 'injected publication failure'); END""")
    with pytest.raises(ConflictError):
        store.commit_node(node)
    assert store.list(Node) == (c0,)
    assert store.check_integrity().incomplete_steps == (step.step_id,)
    store._db.execute("DROP TRIGGER fail_node")
    assert store.commit_node(node) == node
    assert len(store.list(Execution)) == 1


def test_process_death_between_insert_and_commit_does_not_publish_node(store, tmp_path):
    run, attempt, state, c0 = start(store)
    step = begin_step(store, run, attempt, c0)
    model = call(store, step, kind="model")
    node = candidate(store, step, state, model)
    node_file = tmp_path / "candidate.json"
    node_file.write_text(node.model_dump_json())
    store.close()
    script = """
import os, sys
from pathlib import Path
from palinreplay.storage.store import RecordStore
from palinreplay.core.models import Node
store = RecordStore.open(sys.argv[1], read_only=False)
insert = store._insert
def crash(record):
    insert(record)
    os._exit(77)
store._insert = crash
store.commit_node(Node.model_validate_json(Path(sys.argv[2]).read_bytes()))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(store.root), str(node_file)],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 77, result.stderr
    # A writer can recover SQLite's hot journal left by the killed process.
    with RecordStore.open(store.root, read_only=False) as recovered:
        assert recovered.list(Node) == (c0,)
        assert recovered.check_integrity().ok
        assert recovered.check_integrity().incomplete_steps == (step.step_id,)
        assert recovered.commit_node(node) == node


def test_artifact_flush_failure_cannot_publish_or_register_material(store, monkeypatch):
    def fail(fd):
        raise OSError("injected disk failure")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="disk failure"):
        store.put_bytes(b"never published")
    assert list((store.root / "artifacts").iterdir()) == [store.root / "artifacts" / ".tmp"]
    assert not list((store.root / "artifacts" / ".tmp").iterdir())


def test_corrupt_existing_object_is_not_silently_repaired(store):
    ref = store.put_bytes(b"original")
    path = artifact_path(store, ref)
    path.chmod(0o600)
    path.write_bytes(b"modified")
    with pytest.raises(IntegrityError, match="hash mismatch"):
        store.put_bytes(b"original")
    assert path.read_bytes() == b"modified"


def test_metadata_corruption_and_unsupported_store_schema_are_detected(store):
    run, _, _, _ = start(store)
    store._db.execute("UPDATE records SET body=? WHERE kind='run' AND id=?", ("{}", run.run_id))
    with pytest.raises(IntegrityError, match="metadata hash"):
        store.get(Run, run.run_id)
    assert not store.check_integrity().ok
    store._db.execute("UPDATE metadata SET value='future' WHERE key='schema_version'")
    with pytest.raises(IntegrityError, match="schema version"):
        RecordStore.open(store.root)


def test_failure_categories_and_staging_tail_survive_reopen(store):
    run, attempt, _, _ = start(store)
    for category in ("execution", "recording", "cleanup"):
        store.save(
            Failure(
                run_id=run.run_id,
                attempt_id=attempt.attempt_id,
                category=category,
                details=store.put_json({"error": category}),
            )
        )
    (store.root / "artifacts" / ".tmp" / "interrupted.partial").write_bytes(b"partial")
    store.close()
    with RecordStore.open(store.root) as reopened:
        assert [f.category for f in reopened.list(Failure)] == ["execution", "recording", "cleanup"]
        report = reopened.check_integrity()
        assert report.ok and report.staged_artifacts == ("interrupted.partial",)


def test_open_never_creates_or_replaces_existing_storage(tmp_path):
    path = tmp_path / "absent"
    with pytest.raises(IntegrityError):
        RecordStore.open(path)
    assert not path.exists()
    with RecordStore.create(path):
        pass
    with pytest.raises(FileExistsError):
        RecordStore.create(path)
    (path / "catalog.sqlite").unlink()
    with pytest.raises(IntegrityError):
        RecordStore.open(path, read_only=False)


def test_two_writers_cannot_publish_different_nodes_for_same_boundary(store):
    run, attempt, state, c0 = start(store)
    step = begin_step(store, run, attempt, c0)
    model = call(store, step, kind="model")
    first = candidate(store, step, state, model)
    second = candidate(store, step, state, model)
    with RecordStore.open(store.root, read_only=False) as other:
        store.commit_node(first)
        with pytest.raises(ConflictError):
            other.commit_node(second)
        assert other.get_node(first.node_id) == first
