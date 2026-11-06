import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from palinreplay.core.interfaces import AgentSnapshot, BoundaryAdapter, Payload, ResultCapture
from palinreplay.core.models import Event, Failure, Node, RunEnd, State, Step, ToolGroup
from palinreplay.record.calls import RecordingError, RecordingStoppedError, read_call
from palinreplay.record.recorder import Recorder
from palinreplay.storage.store import ConflictError, IncompleteRecordError, RecordStore
from tests.helpers import recorder_smoke as fake

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def context(tmp_path):
    with RecordStore.create(tmp_path / "record") as store:
        agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
        rec = fake.recorder(store, agent, backend)
        yield store, rec, agent, backend


def assert_prefix(store, rec, size=1):
    nodes = store.list(Node)
    assert len(nodes) == size
    assert rec.current_node == nodes[-1] if nodes else rec.current_node is None
    for node in nodes:
        assert store.get_node(node.node_id) == node
    assert store.check_integrity().ok


def test_incomplete_end_preserves_tail_and_structured_native_status(context):
    store, rec, _, _ = context
    c0 = rec.start()
    rec.begin_step()
    end = rec.end_incomplete("native early submission", details={"agent_exit_status": "Submitted"})
    assert end.status == "incomplete" and end.last_node_id == c0.node_id
    assert store.read_json(end.details)["agent_exit_status"] == "Submitted"
    assert store.check_integrity().incomplete_steps and store.check_integrity().ok
    rec.close()


def test_multi_step_fixture_reopens_without_agent_or_environment(tmp_path):
    report = fake.main(tmp_path / "smoke")
    assert report["status"] == "passed"
    assert report["state_captures"] == 4
    assert report["normal_tool_calls"] == 4


def test_c0_must_be_published_before_first_model_dispatch(context):
    store, rec, agent, backend = context
    with pytest.raises(RecordingStoppedError):
        rec.begin_step()
    assert not store.list(Node) and not store.list(Event)
    c0 = rec.start()
    assert store.read_json(c0.context)["messages"] == agent.messages
    seen = []

    def execute(request):
        assert store.get_node(c0.node_id) == c0
        seen.append(request)
        return {"role": "assistant", "tool_calls": []}

    step = rec.begin_step()
    call = step.model(replace(fake.model_adapter(), execute=execute), {})
    response = call.invoke()
    fake.adopt(agent, call, response)
    step.declare_tools(call, ())
    c1 = rec.commit_step()
    assert len(seen) == 1 and len(backend.captures) == 2
    assert c1.parent_node_id == c0.node_id and c1.accepted_event_sequence == 4
    assert c1.model_execution_id == call.execution_id and not c1.tool_execution_ids


@pytest.mark.parametrize("failure", ["backend", "incomplete", "missing", "wrong-type"])
def test_c0_capture_failure_never_exposes_model_entry(context, failure):
    store, rec, _, backend = context

    def capture(store, boundary, previous):
        if failure == "backend":
            raise OSError("fixture capture failed")
        if failure == "wrong-type":
            return None
        state = backend.capture(store, boundary, previous)
        if failure == "incomplete":
            state = state.model_copy(
                update={"capture": state.capture.model_copy(update={"complete": False})}
            )
        else:
            ref = state.artifacts[0]
            (store.root / "artifacts" / ref.sha256[:2] / ref.sha256[2:]).unlink()
        return state

    rec.boundary = BoundaryAdapter(rec.boundary.export_agent, capture)
    with pytest.raises(RecordingError):
        rec.start()
    assert not store.list(Node) and not store.list(Event)
    assert store.list(RunEnd)[0].last_node_id is None
    assert store.list(RunEnd)[0].status == "failed"
    with pytest.raises(RecordingStoppedError):
        rec.begin_step()


@pytest.mark.parametrize(
    "tail",
    [
        "no-model",
        "undeclared",
        "missing-tool",
        "prepared",
        "returned",
        "delivered",
        "unknown",
    ],
)
def test_incomplete_steps_never_capture_or_publish(context, tail):
    store, rec, agent, backend = context
    rec.start()
    if tail == "no-model":
        step = rec.begin_step()
    elif tail == "undeclared":
        step = rec.begin_step()
        model = step.model(fake.model_adapter(), {"tool_calls": []})
        fake.adopt(agent, model, model.invoke())
    else:
        step = fake.answer(rec, agent, ("t1", "t2") if tail == "missing-tool" else ("t1",))
        call = step.tool(fake.tool_adapter(backend), {"action": "read"}, tool_call_id="t1")
        if tail == "unknown":
            call._adapter = replace(
                call._adapter,
                capture_result=lambda value: ResultCapture(
                    Payload.json(value), "fixture-unknown", outcome="unknown"
                ),
            )
        if tail != "prepared":
            value = call.invoke()
            if tail == "delivered":
                call.deliver(value, encode=Payload.json, receive=agent.receive)
            elif tail != "returned":
                fake.adopt(agent, call, value)
    with pytest.raises(RecordingError) as caught:
        rec.commit_step()
    assert isinstance(caught.value.recording_error, IncompleteRecordError)
    assert len(backend.captures) == 1
    assert_prefix(store, rec)
    assert store.check_integrity().incomplete_steps == (step.step.step_id,)
    assert rec.end.status == "failed"
    with pytest.raises(RecordingStoppedError):
        rec.begin_step()


def test_declared_group_survives_reopen_and_store_rejects_omitted_tool(context):
    store, rec, agent, _ = context
    c0 = rec.start()
    step = fake.answer(rec, agent, ("not-invoked",))
    group = store.list(ToolGroup)[0]
    with RecordStore.open(store.root) as reader:
        assert reader.get(ToolGroup, group.toolgroup_id).tool_call_ids == ("not-invoked",)
    forged = Node(
        run_id=rec.run.run_id,
        attempt_id=rec.attempt.attempt_id,
        index=1,
        parent_node_id=c0.node_id,
        step_id=step.step.step_id,
        context=c0.context,
        input_position=c0.input_position,
        state_id=c0.state_id,
        model_execution_id=group.model_execution_id,
        accepted_event_sequence=4,
    )
    with pytest.raises(IncompleteRecordError, match="complete tool group"):
        store.commit_node(forged)
    assert_prefix(store, rec)


def test_tool_group_cannot_be_changed_and_undeclared_calls_do_not_execute(context):
    store, rec, agent, backend = context
    rec.start()
    step = fake.answer(rec, agent, ("allowed",))
    with pytest.raises(ConflictError):
        step.tool(fake.tool_adapter(backend), {"action": "read"}, tool_call_id="extra")
    with pytest.raises(ConflictError):
        step.model(fake.model_adapter(), {"tool_calls": []})
    group = store.list(ToolGroup)[0]
    with pytest.raises(ConflictError):
        store.save(group.model_copy(update={"tool_call_ids": ()}))
    assert backend.tool_calls == 0


def test_known_tool_exception_can_be_adopted_and_committed(context):
    store, rec, agent, backend = context
    rec.start()
    step = fake.answer(rec, agent, ("t1",))
    error = ValueError("known backend rejection")

    def execute(request):
        raise error

    adapter = replace(
        fake.tool_adapter(backend),
        execute=execute,
        capture_exception=lambda exc: ResultCapture(
            Payload.json({"error": str(exc)}), "fixture-error", outcome="error"
        ),
    )
    call = step.tool(adapter, {}, tool_call_id="t1")
    with pytest.raises(ValueError) as caught:
        call.invoke()
    assert caught.value is error
    fake.adopt(agent, call, {"role": "tool", "error": str(error)})
    node = rec.commit_step()
    assert store.get_node(node.node_id) == node
    assert any(f.category == "execution" for f in store.list(Failure))
    assert rec.finish().status == "completed"


@pytest.mark.parametrize("kind", ["model", "tool"])
def test_explicit_known_error_retry_keeps_both_attempts(context, kind):
    store, rec, agent, _ = context
    rec.start()
    step = rec.begin_step() if kind == "model" else fake.answer(rec, agent, ("t1",))
    count = []

    def execute(request):
        count.append(request)
        if len(count) == 1:
            raise ValueError("known failure before a result")
        return {"role": "assistant" if kind == "model" else "tool", "tool_calls": []}

    adapter = replace(
        fake.model_adapter(),
        execute=execute,
        capture_exception=lambda error: ResultCapture(
            Payload.json({"error": str(error)}), "known-error", outcome="error"
        ),
    )
    call = step.model(adapter, {}) if kind == "model" else step.tool(adapter, {}, tool_call_id="t1")
    with pytest.raises(ValueError):
        call.invoke()
    retry = step.retry(call)
    fake.adopt(agent, retry, retry.invoke())
    if kind == "model":
        step.declare_tools(retry, ())
    node = rec.commit_step()
    assert len(count) == 2
    assert call.operation == retry.operation and call.execution_id != retry.execution_id
    assert read_call(store, call.execution_id).phase == "result"
    assert store.get_node(node.node_id) == node


def test_shared_state_still_captures_empty_answer_and_detects_mode_change(context):
    store, rec, agent, backend = context
    c0 = rec.start()
    fake.answer(rec, agent, ())
    c1 = rec.commit_step()
    assert c1.state_id == c0.state_id and c1.context != c0.context
    fake.answer(rec, agent, ())
    backend.files["/testbed/a"]["mode"] = 0o700
    c2 = rec.commit_step()
    assert c2.state_id != c1.state_id and len(backend.captures) == 3
    old = store.get(State, c0.state_id)
    assert store.read_json(old.artifacts[0])["/testbed/a"]["mode"] == 0o644


@pytest.mark.parametrize("drift", ["context", "position"])
def test_agent_drift_during_state_capture_prevents_publication(context, drift):
    store, rec, agent, backend = context
    rec.start()
    fake.answer(rec, agent, ())

    def capture(store, boundary, previous):
        state = backend.capture(store, boundary, previous)
        if drift == "context":
            agent.messages.append({"role": "user", "content": "concurrent input"})
        else:
            agent.input_position += 1
        return state

    rec.boundary = BoundaryAdapter(agent.export, capture)
    with pytest.raises(RecordingError, match="boundary-check"):
        rec.commit_step()
    assert_prefix(store, rec)


def test_capture_closes_existing_handles_and_all_lifecycle_entries(context):
    store, rec, agent, backend = context
    rec.start()
    step = fake.answer(rec, agent, ("t1",))
    call = step.tool(fake.tool_adapter(backend), {"action": "read"}, tool_call_id="t1")
    fake.adopt(agent, call, call.invoke())

    def capture(store, boundary, previous):
        for action in (rec.begin_step, rec.commit_step, rec.finish, rec.cancel, rec.close):
            with pytest.raises(RuntimeError, match="reentrant"):
                action()
        with pytest.raises(RecordingStoppedError):
            call.invoke()
        with pytest.raises(RecordingStoppedError):
            call.accept({}, encode=Payload.json)
        return backend.capture(store, boundary, previous)

    rec.boundary = BoundaryAdapter(agent.export, capture)
    node = rec.commit_step()
    assert store.get_node(node.node_id) == node and backend.tool_calls == 1
    assert backend.captures[-1].accepted_event_sequence == node.accepted_event_sequence
    assert backend.captures[-1].node_id == node.node_id
    with pytest.raises(RecordingStoppedError):
        step.model(fake.model_adapter(), {})


@pytest.mark.parametrize("at", ["capture", "context-write", "commit"])
def test_capture_and_publication_failure_keep_prefix_and_stop_next_step(context, monkeypatch, at):
    store, rec, agent, _ = context
    rec.start()
    fake.answer(rec, agent, ())
    error = OSError("injected recorder failure")

    def fail(*args, **kwargs):
        raise error

    if at == "capture":
        rec.boundary = BoundaryAdapter(agent.export, fail)
    elif at == "context-write":
        real = store.put_payload
        wanted = agent.export(None).context

        def put(payload):
            if payload == wanted:
                raise error
            return real(payload)

        monkeypatch.setattr(store, "put_payload", put)
    else:
        # Fail after insertion but before COMMIT to exercise the real transaction rollback.
        real = store._insert

        def insert(record):
            real(record)
            if isinstance(record, Node):
                raise error

        monkeypatch.setattr(store, "_insert", insert)
    with pytest.raises(RecordingError) as caught:
        rec.commit_step()
    assert caught.value.recording_error is error
    assert_prefix(store, rec)
    assert rec.end.status == "failed"
    with pytest.raises(RecordingStoppedError):
        rec.begin_step()


def test_call_recording_failure_latches_entire_recorder(context):
    store, rec, _, _ = context
    rec.start()
    step = rec.begin_step()
    bad = replace(fake.model_adapter(), capture_result=lambda value: None)
    model = step.model(bad, {"tool_calls": []})
    with pytest.raises(RecordingError):
        model.invoke()
    assert rec.phase == "stopped" and rec.end.status == "failed"
    assert_prefix(store, rec)
    with pytest.raises(RecordingStoppedError):
        rec.commit_step()


def test_total_write_failure_preserves_original_and_diagnostic_errors(context, monkeypatch):
    store, rec, agent, _ = context
    rec.start()
    fake.answer(rec, agent, ())
    error = OSError("disk unavailable")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(store._artifacts, "put", fail)
    with pytest.raises(RecordingError) as caught:
        rec.commit_step()
    assert caught.value.recording_error is error
    assert caught.value.diagnostic_error is error
    assert rec.phase == "stopped" and rec.end is None
    assert any("termination could not be persisted" in n for n in error.__notes__)
    assert_prefix(store, rec)


@pytest.mark.parametrize("status", ["completed", "paused", "cancelled"])
def test_terminal_status_persists_and_freezes_attempt(context, status):
    store, rec, agent, _ = context
    rec.start()
    step = fake.answer(rec, agent, ())
    if status != "cancelled":
        rec.commit_step()
    end = {"completed": rec.finish, "paused": rec.pause, "cancelled": rec.cancel}[status]()
    rec.close()
    rec.close()
    with RecordStore.open(store.root) as reopened:
        assert reopened.get(RunEnd, end.runend_id).status == status
        assert bool(reopened.check_integrity().incomplete_steps) == (status == "cancelled")
    assert len(store.list(RunEnd)) == 1  # Store is still owned/open by the caller.
    with pytest.raises(ConflictError, match="ended"):
        store.save(
            Step(
                run_id=rec.run.run_id,
                attempt_id=rec.attempt.attempt_id,
                index=rec.current_node.index + 1,
                parent_node_id=rec.current_node.node_id,
            )
        )
    with pytest.raises(RecordingStoppedError):
        step.model(fake.model_adapter(), {})


def test_finish_cannot_silently_publish_or_drop_an_active_step(context):
    store, rec, agent, backend = context
    rec.start()
    fake.answer(rec, agent, ())
    with pytest.raises(RecordingStoppedError):
        rec.finish()
    assert len(backend.captures) == 1 and not store.list(RunEnd)
    rec.close()
    assert rec.end.status == "cancelled"
    assert store.check_integrity().incomplete_steps


def test_tool_cancellation_keeps_unknown_tail_without_capture_or_retry(tmp_path):
    error = KeyboardInterrupt("cancel inside backend")
    entries = []

    def execute(request):
        entries.append(request)
        raise error

    with RecordStore.create(tmp_path / "record") as store:
        agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
        rec = fake.recorder(store, agent, backend)
        with pytest.raises(KeyboardInterrupt) as caught, rec:
            step = fake.answer(rec, agent, ("cancel",))
            call = step.tool(
                replace(fake.tool_adapter(backend), execute=execute), {}, tool_call_id="cancel"
            )
            call.invoke()
        assert caught.value is error and len(entries) == 1
        assert rec.end.status == "cancelled" and len(backend.captures) == 1
        assert read_call(store, call.execution_id).phase == "unknown"
        assert store.check_integrity().pending_executions == (call.execution_id,)
        assert_prefix(store, rec)


def test_end_write_failure_stops_recording_without_claiming_clean_completion(context, monkeypatch):
    store, rec, _, _ = context
    rec.start()
    real = store.save

    def save(record):
        if isinstance(record, RunEnd):
            raise OSError("end marker unavailable")
        return real(record)

    monkeypatch.setattr(store, "save", save)
    with pytest.raises(RecordingError, match="termination"):
        rec.finish()
    assert rec.phase == "stopped" and rec.end is None and not store.list(RunEnd)
    assert_prefix(store, rec)
    with pytest.raises(RecordingStoppedError):
        rec.begin_step()


@pytest.mark.parametrize("exception", [ValueError("caller failed"), KeyboardInterrupt()])
def test_context_exit_preserves_primary_error_and_records_cleanup_separately(tmp_path, exception):
    cleaned = []

    def cleanup():
        cleaned.append(True)
        raise OSError("cleanup failed")

    with RecordStore.create(tmp_path / "record") as store:
        agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
        rec = fake.recorder(store, agent, backend, cleanup=cleanup)
        with pytest.raises(type(exception)) as caught, rec:
            fake.answer(rec, agent, ("never-called",))
            raise exception
        assert caught.value is exception and cleaned == [True]
        rec.close()
        assert cleaned == [True]
        assert any("cleanup also failed" in note for note in exception.__notes__)
        assert [f.category for f in store.list(Failure)] == ["execution", "cleanup"]
        assert rec.end.status == ("failed" if isinstance(exception, Exception) else "cancelled")
        assert_prefix(store, rec)


def test_enter_failure_still_cleans_explicit_resources_once(tmp_path):
    cleaned = []
    with RecordStore.create(tmp_path / "record") as store:
        agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
        rec = fake.recorder(store, agent, backend, cleanup=lambda: cleaned.append(True))
        rec.boundary = BoundaryAdapter(lambda boundary: None, backend.capture)
        with pytest.raises(RecordingError), rec:
            pytest.fail("start must fail before exposing a live Recorder")
        rec.close()
        assert cleaned == [True] and not store.list(Node)


def test_clean_context_exit_pauses_and_cleanup_error_does_not_relabel_recording(tmp_path):
    def cleanup():
        raise OSError("owned resource cleanup failed")

    with RecordStore.create(tmp_path / "record") as store:
        agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
        rec = fake.recorder(store, agent, backend, cleanup=cleanup)
        with pytest.raises(OSError, match="cleanup"), rec:
            pass
        assert rec.end.status == "paused"
        assert store.list(Failure)[0].category == "cleanup"
        assert_prefix(store, rec)


def test_backend_cannot_reenter_lifecycle_or_move_it_to_another_thread(context):
    store, rec, _, _ = context
    rec.start()
    errors = []

    def other_thread():
        try:
            rec.begin_step()
        except RuntimeError as error:
            errors.append(error)

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join()
    assert len(errors) == 1 and not store.list(Step)
    step = rec.begin_step()

    def execute(request):
        with pytest.raises(RuntimeError, match="reentrant"):
            rec.cancel()
        return {"role": "assistant", "tool_calls": []}

    step.model(replace(fake.model_adapter(), execute=execute), {}).invoke()
    assert rec.phase == "step"


def test_async_capture_contract_is_rejected_and_dynamic_awaitable_not_consumed(context):
    store, rec, _, backend = context
    entered = []

    async def export(boundary):
        entered.append(True)
        return AgentSnapshot(Payload.json([]), Payload.json(0))

    with pytest.raises(TypeError, match="synchronous"):
        Recorder(
            store,
            task_id="async",
            agent_name="fake",
            agent_version="1",
            config=Payload.json({}),
            boundary=BoundaryAdapter(export, backend.capture),
        )
    rec.boundary = BoundaryAdapter(lambda boundary: export(boundary), backend.capture)
    with pytest.raises(RecordingError):
        rec.start()
    assert not entered and not store.list(Node)


@pytest.mark.parametrize("crash_at", ["capture", "node-insert"])
def test_process_death_leaves_readable_prefix_without_replaying_calls(tmp_path, crash_at):
    path = tmp_path / "crashed"
    script = """
import os, sys
from tests.helpers.recorder_smoke import FakeAgent, FakeStateBackend, recorder, answer
from palinreplay.storage.store import RecordStore
from palinreplay.core.models import Node
from palinreplay.core.interfaces import BoundaryAdapter
store = RecordStore.create(sys.argv[1])
agent, backend = FakeAgent(), FakeStateBackend()
rec = recorder(store, agent, backend)
rec.start()
answer(rec, agent, ())
if sys.argv[2] == "capture":
    def capture(*args):
        os._exit(73)
    rec.boundary = BoundaryAdapter(agent.export, capture)
else:
    real = store._insert
    def insert(record):
        real(record)
        if isinstance(record, Node):
            os._exit(73)
    store._insert = insert
rec.commit_step()
"""
    child = subprocess.run([sys.executable, "-c", script, str(path), crash_at], cwd=ROOT)
    assert child.returncode == 73
    with RecordStore.open(path, read_only=False) as store:
        assert len(store.list(Node)) == 1 and not store.list(RunEnd)
        assert len(store.list(Event)) == 4
        assert store.get_node(store.list(Node)[0].node_id)
        integrity = store.check_integrity()
        assert integrity.ok and len(integrity.incomplete_steps) == 1
