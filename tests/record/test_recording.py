import asyncio
import hashlib
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import datetime
from functools import partial

import pytest

from palinreplay.core.interfaces import CallAdapter, Payload, ResultCapture
from palinreplay.core.models import (
    ArtifactRef,
    Attempt,
    CallEvidence,
    CaptureInfo,
    Event,
    Execution,
    Node,
    Run,
    State,
    Step,
)
from palinreplay.record.calls import (
    RecordingError,
    RecordingPorts,
    RecordingStoppedError,
    deliver_batch,
    read_call,
)
from palinreplay.sandbox.base import ExecResult
from palinreplay.storage.store import ConflictError, IncompleteRecordError, RecordStore


def test_batch_offers_are_durable_before_one_native_handoff(context):
    store, ports, _, _ = context
    calls = [ports.model(json_adapter(), {"i": i}) for i in range(2)]
    for call in calls:
        call.invoke()
    observed = []
    messages = [{"content": "one"}, {"content": "two"}]

    def receive(values):
        for value in values:
            payload = Payload.json(value)
            ref = ArtifactRef(sha256=hashlib.sha256(payload.data).hexdigest(),
                              size=len(payload.data), media_type=payload.media_type)
            assert store.read_json(ref) == value
        observed.append(values)
        return "native-return"

    assert deliver_batch(list(zip(calls, messages)), encode=Payload.json, receive=receive) == "native-return"
    assert observed == [messages]
    assert [read_call(store, call.execution_id).phase for call in calls] == ["delivered"] * 2
    with pytest.raises(ConflictError):
        deliver_batch(list(zip(calls, messages)), encode=Payload.json, receive=receive)
    assert len(observed) == 1


def test_batch_persistence_failure_does_not_repeat_receiver(context, monkeypatch):
    store, ports, _, _ = context
    calls = [ports.model(json_adapter(), {"i": i}) for i in range(2)]
    for call in calls:
        call.invoke()
    received = []
    append = store.append_event

    def fail(execution_id, kind, **kwargs):
        if kind == "delivered" and execution_id == calls[1].execution_id:
            raise OSError("second delivery write failed")
        return append(execution_id, kind, **kwargs)

    monkeypatch.setattr(store, "append_event", fail)
    with pytest.raises(RecordingError):
        deliver_batch([(call, {}) for call in calls], encode=Payload.json, receive=received.append)
    assert len(received) == 1
    assert read_call(store, calls[0].execution_id).phase == "delivered"
    assert read_call(store, calls[1].execution_id).phase == "result"
    with pytest.raises(RecordingStoppedError):
        calls[1].deliver({}, encode=Payload.json, receive=received.append)


def test_internal_evidence_is_ordered_and_frozen_with_execution(context):
    store, ports, _, _ = context

    def execute(request):
        call.observe("request", Payload.json({"prepared": request}))
        call.observe("response", Payload.json({"response": "raw"}))
        return {"content": "parsed"}

    call = ports.model(json_adapter(execute), {})
    call.invoke()
    trace = read_call(store, call.execution_id)
    assert [e.index for e in trace.evidence] == [1, 2]
    with pytest.raises(ConflictError):
        store.save(CallEvidence(run_id=ports.step.run_id, execution_id=call.execution_id,
                                index=3, kind="response", payload=store.put_json({})))
    assert store.check_integrity().ok


@pytest.fixture
def context(tmp_path):
    with RecordStore.create(tmp_path / "record") as store:
        run = store.save(
            Run(
                task_id="fixture",
                agent_name="fake",
                agent_version="1",
                config=store.put_json({"network": "none"}),
            )
        )
        attempt = store.save(Attempt(run_id=run.run_id))
        state = store.save(
            State(
                backend="fixture",
                capture=CaptureInfo(
                    scope="fixture",
                    included=("/testbed",),
                    attributes=("content",),
                    consistency="fixture",
                    complete=True,
                ),
                manifest=store.put_json({"paths": []}),
                runtime=store.put_json({"network": "none"}),
                artifacts=(store.put_bytes(b"fixture state"),),
            )
        )
        c0 = store.commit_node(
            Node(
                run_id=run.run_id,
                attempt_id=attempt.attempt_id,
                index=0,
                context=store.put_json([]),
                input_position=store.put_json(0),
                state_id=state.state_id,
                accepted_event_sequence=0,
            )
        )
        step = store.save(
            Step(
                run_id=run.run_id, attempt_id=attempt.attempt_id, index=1, parent_node_id=c0.node_id
            )
        )
        yield store, RecordingPorts(store, step.step_id), state, c0


def json_adapter(execute=lambda request: {"content": "answer"}, **changes):
    adapter = CallAdapter(
        name="fake.model",
        execute=execute,
        capture_request=Payload.json,
        capture_result=lambda result: ResultCapture(
            raw=Payload.json(result),
            source="model-api-json",
            limitations=("HTTP wire bytes and hidden reasoning are not exposed.",),
        ),
        visibility="observed-attempt",
    )
    return replace(adapter, **changes)


def accept(call, value):
    # The fake Agent really adopts a changed value; the return alone is not acceptance.
    messages = []

    def receive(observation):
        messages.append({"adopted": observation})
        return messages[-1]

    receipt = call.deliver(value, encode=Payload.json, receive=receive)
    assert receipt is messages[-1]
    call.accept(messages[-1], encode=Payload.json)
    return messages


def candidate(store, ports, state, c0, model, tools=()):
    return Node(
        run_id=ports.step.run_id,
        attempt_id=ports.step.attempt_id,
        index=1,
        parent_node_id=c0.node_id,
        step_id=ports.step.step_id,
        context=store.put_json({"messages": "fixture"}),
        input_position=store.put_json(4),
        state_id=state.state_id,
        accepted_event_sequence=store.list(Event)[-1].sequence,
        model_execution_id=model.execution_id,
        tool_operation_ids=tuple(tool.operation.operation_id for tool in tools),
        tool_execution_ids=tuple(tool.execution_id for tool in tools),
    )


def test_model_inputs_complete_response_and_usage_survive_reopen(context):
    store, ports, state, c0 = context
    messages = [{"role": "user", "content": "你好\r\n"}]
    request = {"messages": messages, "model": "fake-v1", "temperature": 0.2, "seed": 7}
    response = {
        "id": "response-1",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "t1", "function": {"name": "shell", "arguments": "{}"}}],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 8},
        "provider_extension": {"keep": True},
    }
    seen = []

    def execute(actual):
        assert actual is request and actual["messages"] is messages
        assert store.list(Event)[-1].kind == "dispatched"
        seen.append(actual)
        return response

    call = ports.model(json_adapter(execute), request)
    assert not seen and read_call(store, call.execution_id).phase == "prepared"
    value = call.invoke()
    assert value is response and len(seen) == 1
    trace = read_call(store, call.execution_id)
    assert [e.kind for e in trace.events] == ["dispatched", "result"]
    assert store.read_json(trace.execution.request) == request
    assert store.read_json(trace.events[-1].payload) == response
    timing = store.read_json(trace.events[-1].details)["timing"]
    assert datetime.fromisoformat(timing["finished_at"]) >= datetime.fromisoformat(
        timing["started_at"]
    )
    assert timing["duration_sec"] >= 0
    with pytest.raises(IncompleteRecordError):
        store.commit_node(candidate(store, ports, state, c0, call))
    accept(call, {"content": "Agent extracted the tool call"})
    node = store.commit_node(candidate(store, ports, state, c0, call))
    response["provider_extension"]["keep"] = False
    store.close()
    with RecordStore.open(store.root) as reopened:
        trace = read_call(reopened, call.execution_id)
        assert trace.phase == "accepted"
        assert reopened.read_json(trace.events[1].payload)["provider_extension"] == {"keep": True}
        assert reopened.read_json(trace.events[-1].payload) == {
            "adopted": {"content": "Agent extracted the tool call"}
        }
        assert reopened.get_node(node.node_id) == node
        assert reopened.check_integrity().ok


def test_tool_bytes_exit_code_context_and_actual_adoption_are_separate(context):
    store, ports, _, _ = context
    request = {
        "command": "fixture command",
        "cwd": "/testbed",
        "env": {"LANG": "C"},
        "timeout_sec": 3,
    }
    result = ExecResult(request["command"], b"out\x00\xff\r\nerror\r\n", 7, 0.1)
    adapter = CallAdapter(
        name="fake.shell",
        execute=lambda actual: result,
        capture_request=Payload.json,
        capture_context=lambda actual: Payload.json(
            {"cwd": actual["cwd"], "shell": ["bash", "-c"]}
        ),
        capture_result=lambda actual: ResultCapture(
            raw=Payload(actual.output),
            source="merged-tool-output",
            details={
                "exit_code": actual.exit_code,
                "duration_sec": actual.duration_sec,
                "streams": "stdout/stderr merged",
            },
        ),
    )
    call = ports.tool(adapter, request, tool_call_id="provider-call-1")
    assert call.invoke() is result
    observation = {"text": result.text[:4]}
    accept(call, observation)
    trace = read_call(store, call.execution_id)
    assert trace.execution.visibility == "opaque-retries"
    assert store.read_json(trace.execution.request) == request
    assert store.read_json(trace.execution.context) == {"cwd": "/testbed", "shell": ["bash", "-c"]}
    assert store.read_bytes(trace.events[1].payload) == result.output
    assert store.read_json(trace.events[1].details)["backend"]["exit_code"] == 7
    assert trace.events[1].outcome == "returned"
    assert store.read_json(trace.events[2].payload) == observation
    assert store.read_json(trace.events[3].payload) == {"adopted": observation}


def test_tool_group_keeps_distinct_calls_cause_and_actual_delivery_order(context):
    store, ports, state, c0 = context
    model = ports.model(json_adapter(), {})
    accept(model, model.invoke())
    first = ports.tool(
        json_adapter(), {}, tool_call_id="first", caused_by_execution_id=model.execution_id
    )
    second = ports.tool(
        json_adapter(), {}, tool_call_id="second", caused_by_execution_id=model.execution_id
    )
    one, two = first.invoke(), second.invoke()
    accept(second, {"second": two})
    accept(first, {"first": one})
    assert first.execution_id != second.execution_id
    assert first.execution.request == second.execution.request
    assert first.operation.caused_by_execution_id == model.execution_id
    delivered = [e.execution_id for e in store.list(Event) if e.kind == "delivered"]
    assert delivered == [model.execution_id, second.execution_id, first.execution_id]
    node = store.commit_node(candidate(store, ports, state, c0, model, (first, second)))
    assert store.get_node(node.node_id) == node


def test_handoff_does_not_imply_adoption_and_serialize_before_receiver_mutation(context):
    store, ports, state, c0 = context
    call = ports.model(json_adapter(), {})
    call.invoke()
    offered = {"content": "original"}

    def receive(value):
        value["content"] = "what the Agent adopted"
        return value

    receipt = call.deliver(offered, encode=Payload.json, receive=receive)
    assert receipt is offered
    assert read_call(store, call.execution_id).phase == "delivered"
    with pytest.raises(IncompleteRecordError):
        store.commit_node(candidate(store, ports, state, c0, call))
    call.accept(receipt, encode=Payload.json)
    trace = read_call(store, call.execution_id)
    assert store.read_json(trace.events[2].payload) == {"content": "original"}
    assert store.read_json(trace.events[3].payload) == {"content": "what the Agent adopted"}


class KnownFailure(Exception):
    pass


def known_exception(error):
    return ResultCapture(
        raw=Payload.json({"error": str(error)}), source="known-backend-rejection", outcome="error"
    )


def test_known_backend_exception_is_rethrown_and_only_explicit_retry_invokes_again(context):
    store, ports, state, c0 = context
    error = KnownFailure("provider rejected the request")
    attempts = []

    def execute(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise error
        return {"content": "retry result"}

    first = ports.model(json_adapter(execute, capture_exception=known_exception), {})
    with pytest.raises(KnownFailure) as caught:
        first.invoke()
    assert caught.value is error and len(attempts) == 1
    assert read_call(store, first.execution_id).events[-1].outcome == "error"
    with pytest.raises(ConflictError):
        first.invoke()
    second = ports.retry(first)
    assert second.operation == first.operation and second.execution.index == 2
    assert second.execution_id != first.execution_id
    accept(second, second.invoke())
    node = store.commit_node(candidate(store, ports, state, c0, second))
    assert store.get_node(node.node_id) == node and len(attempts) == 2
    with pytest.raises(IncompleteRecordError):
        ports.retry(second)


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("lost transport"),
        TimeoutError("deadline"),
        KeyboardInterrupt(),
        SystemExit(3),
        asyncio.CancelledError(),
    ],
)
def test_unknown_failure_timeout_and_cancellation_preserve_original_and_never_retry(context, error):
    store, ports, _, _ = context
    calls = []

    def execute(request):
        calls.append(request)
        raise error

    adapter = json_adapter(execute)
    if isinstance(error, TimeoutError):
        adapter = replace(adapter, capture_exception=known_exception)
    call = ports.model(adapter, {})
    with pytest.raises(type(error)) as caught:
        call.invoke()
    assert caught.value is error and len(calls) == 1
    trace = read_call(store, call.execution_id)
    assert trace.phase == "unknown" and trace.events[-1].outcome == "unknown"
    assert trace.failures[0].category == "execution"
    with pytest.raises(IncompleteRecordError):
        ports.retry(call)
    assert store.check_integrity().pending_executions == (call.execution_id,)


def test_internal_retries_are_not_invented_as_observed_physical_executions(context):
    store, ports, _, _ = context
    hidden = []

    def execute(request):
        hidden.extend(["attempt1", "attempt2"])
        return {"content": "response after internal retries"}

    call = ports.model(json_adapter(execute, visibility="opaque-retries"), {})
    call.invoke()
    assert len(hidden) == 2 and len(store.list(Execution)) == 1
    assert read_call(store, call.execution_id).execution.visibility == "opaque-retries"


def test_missing_result_cannot_be_delivered_or_accepted(context):
    store, ports, _, _ = context
    call = ports.model(json_adapter(), {})
    with pytest.raises(IncompleteRecordError):
        call.deliver({}, encode=Payload.json, receive=lambda value: value)
    with pytest.raises(IncompleteRecordError):
        call.accept({}, encode=Payload.json)
    assert not store.list(Event)


def test_duplicate_invoke_delivery_and_acceptance_do_not_repeat_effects(context):
    _, ports, _, _ = context
    calls, deliveries = [], []
    call = ports.model(json_adapter(lambda request: calls.append(request) or {}), {})
    call.invoke()
    with pytest.raises(ConflictError):
        call.invoke()
    call.deliver({}, encode=Payload.json, receive=lambda value: deliveries.append(value))
    with pytest.raises(ConflictError):
        call.deliver({}, encode=Payload.json, receive=lambda value: deliveries.append(value))
    call.accept({}, encode=Payload.json)
    with pytest.raises(ConflictError):
        call.accept({}, encode=Payload.json)
    assert len(calls) == len(deliveries) == 1


@pytest.mark.parametrize("what", ["request", "context"])
def test_mutated_prepared_input_is_rejected_before_backend_entry(context, what):
    store, ports, _, _ = context
    calls, environment = [], {"cwd": "/testbed"}
    request = {"messages": ["before"]}
    call = ports.model(
        json_adapter(
            lambda request: calls.append(request) or {},
            capture_context=lambda request: Payload.json(environment),
        ),
        request,
    )
    if what == "request":
        request["messages"].append("after")
    else:
        environment["cwd"] = "/elsewhere"
    with pytest.raises(RecordingError) as caught:
        call.invoke()
    assert caught.value.phase == "dispatch" and not calls
    assert read_call(store, call.execution_id).phase == "prepared"


def test_request_encoding_failure_prevents_dispatch_and_stops_ports(context):
    store, ports, _, _ = context
    calls = []
    with pytest.raises(RecordingError) as caught:
        ports.model(
            json_adapter(
                lambda request: calls.append(request), capture_request=lambda request: "not bytes"
            ),
            {},
        )
    assert caught.value.phase == "prepare" and not calls
    assert not store.list(Event) and not store.list(Execution)
    with pytest.raises(RecordingStoppedError):
        ports.model(json_adapter(), {})


@pytest.mark.parametrize("phase", ["dispatched", "result"])
def test_storage_failure_before_or_after_backend_never_causes_reexecution(
    context, monkeypatch, phase
):
    store, ports, _, _ = context
    calls = []
    call = ports.model(
        json_adapter(lambda request: calls.append(request) or {"content": "raw"}), {}
    )
    append = store.append_event

    def fail(execution_id, kind, **kwargs):
        if kind == phase:
            raise OSError("disk unavailable")
        return append(execution_id, kind, **kwargs)

    monkeypatch.setattr(store, "append_event", fail)
    with pytest.raises(RecordingError) as caught:
        call.invoke()
    assert caught.value.phase == ("dispatch" if phase == "dispatched" else "result")
    assert len(calls) == (0 if phase == "dispatched" else 1)
    with pytest.raises(RecordingStoppedError):
        call.invoke()
    assert read_call(store, call.execution_id).phase == (
        "prepared" if phase == "dispatched" else "dispatched"
    )


def test_result_serialization_failure_does_not_return_unrecorded_value(context):
    store, ports, _, _ = context
    calls = []
    call = ports.model(json_adapter(lambda request: calls.append(request) or object()), {})
    with pytest.raises(RecordingError) as caught:
        call.invoke()
    assert caught.value.phase == "result" and len(calls) == 1
    assert read_call(store, call.execution_id).phase == "dispatched"


def test_backend_and_recording_errors_both_survive_when_diagnostics_also_fail(context, monkeypatch):
    store, ports, _, _ = context
    error = KnownFailure("original backend error")
    disk_error = OSError("disk full")

    def execute(request):
        monkeypatch.setattr(
            store._artifacts, "put", lambda *args, **kwargs: (_ for _ in ()).throw(disk_error)
        )
        raise error

    call = ports.model(json_adapter(execute, capture_exception=known_exception), {})
    with pytest.raises(RecordingError) as caught:
        call.invoke()
    assert caught.value.backend_error is error
    assert caught.value.recording_error is disk_error
    assert caught.value.diagnostic_error is disk_error
    assert [e.kind for e in store.list(Event)] == ["dispatched"]


def test_observation_write_failure_prevents_receiver(context, monkeypatch):
    store, ports, _, _ = context
    call = ports.model(json_adapter(), {})
    call.invoke()
    received = []
    put = store.put_payload

    def fail(payload):
        if payload.data == b"offered view":
            raise OSError("observation write failed")
        return put(payload)

    monkeypatch.setattr(store, "put_payload", fail)
    with pytest.raises(RecordingError) as caught:
        call.deliver("view", encode=lambda value: Payload(b"offered view"), receive=received.append)
    assert caught.value.phase == "observation" and not received
    assert read_call(store, call.execution_id).phase == "result"


@pytest.mark.parametrize("failure_phase", ["receiver", "delivered", "accepted"])
def test_handoff_and_acceptance_failure_do_not_fabricate_complete_nodes(
    context, monkeypatch, failure_phase
):
    store, ports, state, c0 = context
    call = ports.model(json_adapter(), {})
    call.invoke()
    messages = []
    append = store.append_event

    def receive(value):
        messages.append(value)
        if failure_phase == "receiver":
            raise ValueError("receiver changed state then failed")
        return messages[-1]

    def fail(execution_id, kind, **kwargs):
        if kind == failure_phase:
            raise OSError(f"cannot persist {kind}")
        return append(execution_id, kind, **kwargs)

    monkeypatch.setattr(store, "append_event", fail)
    with pytest.raises(RecordingError):
        call.deliver({"seen": True}, encode=Payload.json, receive=receive)
        call.accept(messages[-1], encode=Payload.json)
    assert len(messages) == 1
    trace = read_call(store, call.execution_id)
    assert trace.phase == ("delivered" if failure_phase == "accepted" else "result")
    assert store.read_json(trace.failures[-1].details)["handoff_may_have_occurred"]
    with pytest.raises(IncompleteRecordError):
        store.commit_node(candidate(store, ports, state, c0, call))
    with pytest.raises(RecordingStoppedError):
        call.deliver({}, encode=Payload.json, receive=receive)
    assert len(messages) == 1


def test_corrupted_raw_result_blocks_handoff_and_stops_port(context):
    store, ports, _, _ = context
    call = ports.model(json_adapter(), {})
    call.invoke()
    raw = read_call(store, call.execution_id).events[-1].payload
    (store.root / "artifacts" / raw.sha256[:2] / raw.sha256[2:]).unlink()
    received = []
    with pytest.raises(RecordingError) as caught:
        call.deliver({}, encode=Payload.json, receive=received.append)
    assert caught.value.phase == "delivery-check" and not received
    with pytest.raises(RecordingStoppedError):
        ports.model(json_adapter(), {})


@pytest.mark.parametrize("wrapped", ["function", "partial", "callable"])
def test_unsupported_async_backend_rejected_without_dispatch(context, wrapped):
    store, ports, _, _ = context

    async def execute(request):
        raise AssertionError("must not start")

    class AsyncBackend:
        async def __call__(self, request):
            raise AssertionError("must not start")

    backend = {"function": execute, "partial": partial(execute), "callable": AsyncBackend()}[
        wrapped
    ]
    with pytest.raises(TypeError, match="synchronous"):
        ports.model(json_adapter(backend), {})
    assert not store.list(Execution) and not store.list(Event)


@pytest.mark.parametrize("kind", ["coroutine", "generator"])
def test_sync_backend_returning_async_or_stream_does_not_get_consumed(context, kind):
    store, ports, _, _ = context
    entered = []

    async def async_value():
        entered.append(True)
        return {}

    def stream_value():
        entered.append(True)
        yield {}

    adapter = json_adapter(lambda request: async_value() if kind == "coroutine" else stream_value())
    call = ports.model(adapter, {})
    with pytest.raises(RecordingError):
        call.invoke()
    assert not entered and read_call(store, call.execution_id).phase == "dispatched"


def test_cross_thread_and_reentrant_use_rejected_without_serializing_calls(context):
    _, ports, _, _ = context
    backend_calls, errors = [], []
    second = ports.model(json_adapter(lambda request: backend_calls.append("second") or {}), {})

    def foreign_thread():
        try:
            second.invoke()
        except RuntimeError as error:
            errors.append(error)

    thread = threading.Thread(target=foreign_thread)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive() and errors and not backend_calls

    def execute(request):
        with pytest.raises(RuntimeError, match="reentrant"):
            second.invoke()
        backend_calls.append("first")
        return {}

    first = ports.model(json_adapter(execute), {})
    first.invoke()
    second.invoke()
    assert backend_calls == ["first", "second"]


def test_client_credentials_are_not_implicitly_serialized(context):
    store, ports, _, _ = context
    secret = "test-only-private-token-not-to-store"

    class Client:
        def __init__(self):
            self.api_key = secret

        def call(self, request):
            assert self.api_key == secret
            return {"content": "public response"}

    call = ports.model(json_adapter(Client().call), {"model": "public-model"})
    accept(call, call.invoke())
    for path in (store.root / "artifacts").glob("*/*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


@pytest.mark.parametrize("crash_at,phase", [("backend", "dispatched"), ("receiver", "result")])
def test_process_death_keeps_uncertain_tail_without_repeating_side_effect(
    context, tmp_path, crash_at, phase
):
    store, ports, _, _ = context
    marker = tmp_path / "side-effect.txt"
    store.close()
    script = """
import os, sys
from pathlib import Path
from palinreplay.core.interfaces import CallAdapter, Payload, ResultCapture
from palinreplay.record.calls import RecordingPorts
from palinreplay.storage.store import RecordStore

store = RecordStore.open(sys.argv[1], read_only=False)
ports = RecordingPorts(store, sys.argv[2])
def effect():
    Path(sys.argv[3]).write_text('executed once')
    os._exit(77)
def backend(request):
    if sys.argv[4] == 'backend':
        effect()
    return {'content': 'saved result'}
adapter = CallAdapter(name='crash-fixture', execute=backend, capture_request=Payload.json,
    capture_result=lambda value: ResultCapture(raw=Payload.json(value), source='fixture'))
call = ports.model(adapter, {'request': 'fixture'})
result = call.invoke()
call.deliver(result, encode=Payload.json, receive=lambda value: effect())
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(store.root), ports.step.step_id, str(marker), crash_at],
        capture_output=True,
        timeout=15,
    )
    assert process.returncode == 77, process.stderr
    with RecordStore.open(store.root) as reopened:
        execution = reopened.list(Execution)[-1]
        trace = read_call(reopened, execution.execution_id)
        assert trace.phase == phase
        assert not any(event.kind in ("delivered", "accepted") for event in trace.events)
        assert reopened.check_integrity().incomplete_steps == (ports.step.step_id,)
        assert len(reopened.list(Node)) == 1
        assert marker.read_text() == "executed once"


def test_retry_prepared_before_old_attempt_is_accepted_does_not_dispatch(context):
    _, ports, _, _ = context
    calls = []

    def execute(request):
        calls.append(request)
        raise KnownFailure("known failure")

    original = ports.model(json_adapter(execute, capture_exception=known_exception), {})
    with pytest.raises(KnownFailure):
        original.invoke()
    retry = ports.retry(original)
    accept(original, {"error": "Agent accepted the error"})
    with pytest.raises(RecordingError) as caught:
        retry.invoke()
    assert isinstance(caught.value.recording_error, ConflictError)
    assert len(calls) == 1


def test_capture_contracts_reject_implicit_text_fallback_and_invalid_metadata():
    with pytest.raises(TypeError):
        Payload("not bytes")
    with pytest.raises(ValueError):
        ResultCapture(raw=Payload(b"value"), source="")
    with pytest.raises(TypeError):
        ResultCapture(raw=Payload(b"value"), source="fixture", details="not an object")
    with pytest.raises(ValueError):
        CallAdapter(
            name="",
            execute=lambda value: value,
            capture_request=Payload.json,
            capture_result=lambda value: value,
        )


def test_timeout_partial_output_is_preserved_while_outcome_remains_unknown(context):
    store, ports, _, _ = context
    error = TimeoutError("deadline after partial output")

    def execute(request):
        raise error

    call = ports.model(
        json_adapter(
            execute,
            capture_exception=lambda exc: ResultCapture(
                raw=Payload(b"partial\x00\xff\r\n"),
                source="timeout-partial-output",
                outcome="error",
                limitations=("Only output observed before timeout is available.",),
            ),
        ),
        {},
    )
    with pytest.raises(TimeoutError) as caught:
        call.invoke()
    trace = read_call(store, call.execution_id)
    assert caught.value is error and trace.phase == "unknown"
    assert store.read_bytes(trace.events[-1].payload) == b"partial\x00\xff\r\n"
    with pytest.raises(IncompleteRecordError):
        ports.retry(call)
