# ruff: noqa: E402 -- Load the optional SDK through our isolated import boundary first.
import importlib.util
import json
import logging
import os
import subprocess
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("minisweagent") is None:
    pytest.skip("install the mini extra for pinned integration tests", allow_module_level=True)

from palinreplay.agents import AgentTask, get_agent_class
from palinreplay.agents.mini import MiniSweAgent, load_native
from palinreplay.core.models import CallEvidence, CaptureInfo, Failure, Node, State, Step
from palinreplay.llm import LLMConfig
from palinreplay.record.calls import RecordingError, read_call
from palinreplay.sandbox.base import ExecResult, SandboxTimeout
from palinreplay.storage.store import IntegrityError, RecordStore

native = load_native()

from litellm import ModelResponse
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.models import GlobalModelStats
from minisweagent.models import litellm_model as upstream_model
from minisweagent.models.litellm_model import LitellmModel
from minisweagent.models.utils import retry as retry_module
from minisweagent.models.utils.actions_toolcall import BASH_TOOL
from tenacity import wait_none

AGENT = {
    "system_template": "System",
    "instance_template": "{{task}}",
    "cost_limit": 0,
    "step_limit": 10,
}
MODEL = {"model_name": "openai/fixture", "model_kwargs": {"num_retries": 0}}


def response(*commands, malformed=None):
    calls = [
        {
            "id": f"tool-{i}",
            "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({"command": command})},
        }
        for i, command in enumerate(commands)
    ]
    if malformed is not None:
        calls = [
            {"id": "bad", "type": "function", "function": {"name": "bash", "arguments": malformed}}
        ]
    return ModelResponse(
        id="response",
        created=1700000000,
        model="fixture",
        choices=[
            {
                "message": {"role": "assistant", "content": "Thought", "tool_calls": calls or None},
                "finish_reason": "tool_calls" if calls else "stop",
            }
        ],
        usage={"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
    )


class Transport:
    def __init__(self, responses):
        self.responses = deepcopy(responses)
        self.requests = []

    def __call__(self, **request):
        self.requests.append(deepcopy(request))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class Sandbox:
    def __init__(self):
        self.calls = []
        self.files = {"file": "initial"}

    def inspect(self):
        return SimpleNamespace(
            actual_image_id="sha256:fixture",
            workdir="/testbed",
            environment={"PAGER": "cat"},
            shell=("bash", "-c"),
        )

    def exec(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command == "timeout":
            raise SandboxTimeout("unknown tool outcome")
        if command == "cancel":
            raise KeyboardInterrupt()
        if command == "write":
            self.files["file"] = "changed"
        outputs = {
            "": b"",
            "submit": b"  COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\npatch\n",
            "long": b"head\n" + b"m" * 20000 + b"\ntail",
            "bytes": b"a\xff\r\nb\rc",
            "bad": b"known error",
        }
        return ExecResult(command, outputs.get(command, b"ok\n"), int(command == "bad"), 0.25)


class Capture:
    def __init__(self, sandbox):
        self.sandbox, self.calls = sandbox, []

    def __call__(self, store, boundary, previous):
        self.calls.append(boundary)
        data = store.put_json(self.sandbox.files)
        if previous and previous.artifacts == (data,):
            return previous
        return State(
            backend="mini-test-fixture",
            capture=CaptureInfo(
                scope="fixture",
                included=("/testbed",),
                attributes=("content",),
                consistency="fixture",
                complete=True,
            ),
            manifest=data,
            runtime=store.put_json({}),
            artifacts=(data,),
            logical_parent_id=previous.state_id if previous else None,
        )


@pytest.fixture(autouse=True)
def deterministic(monkeypatch):
    uname = native.platform.uname()._asdict()
    monkeypatch.setattr(native.platform, "uname", lambda: SimpleNamespace(_asdict=lambda: uname))
    monkeypatch.setattr(native.time, "time", lambda: 1700000000.0)
    monkeypatch.setattr(
        upstream_model.litellm.cost_calculator, "completion_cost", lambda *a, **k: 0.5
    )
    monkeypatch.setattr(retry_module, "wait_exponential", lambda **kwargs: wait_none())
    monkeypatch.delenv("MSWEA_GLOBAL_COST_LIMIT", raising=False)
    monkeypatch.delenv("MSWEA_GLOBAL_CALL_LIMIT", raising=False)


def integrated(tmp_path, responses, *, agent_config=None, **kwargs):
    store = RecordStore.create(tmp_path / "record")
    sandbox = Sandbox()
    capture = Capture(sandbox)
    transport = Transport(responses)
    agent = MiniSweAgent(
        sandbox=sandbox,
        store=store,
        capture_state=capture,
        model_config=MODEL,
        agent_config=AGENT | (agent_config or {}),
        completion=transport,
        **kwargs,
    )
    return store, sandbox, capture, transport, agent


def original(monkeypatch, responses, recorded, sandbox, agent_config):
    """Run the unmodified native classes; mock only HTTP and docker subprocess I/O."""
    transport = Transport(responses)
    model = LitellmModel(**recorded._model.config.model_dump())
    model._query = lambda messages, **kwargs: transport(
        model=model.config.model_name,
        messages=messages,
        tools=[BASH_TOOL],
        **(model.config.model_kwargs | kwargs),
    )
    env = object.__new__(DockerEnvironment)
    env.config = native.BorrowedEnvironment(sandbox, **recorded._env_config).config
    env.container_id = "fixture-container"
    env.logger = logging.getLogger("fixture")
    env.cleanup = lambda: None

    def docker_exec(argv, **kwargs):
        assert argv[:4] == ["docker", "exec", "-w", "/testbed"]
        assert argv[-3:-1] == ["bash", "-c"]
        result = sandbox.exec(argv[-1], cwd="/testbed", env=env.config.env, timeout_sec=60)
        return subprocess.CompletedProcess(
            argv, result.exit_code, native.BorrowedEnvironment.output(result)["output"]
        )

    monkeypatch.setattr("minisweagent.environments.docker.subprocess.run", docker_exec)
    monkeypatch.setattr(upstream_model, "GLOBAL_MODEL_STATS", GlobalModelStats())
    agent = DefaultAgent(model, env, **(AGENT | agent_config))
    result = agent.run("Task")
    return agent, result, transport


@pytest.mark.parametrize(
    "responses, config, status, nodes",
    [
        (
            [response("write", "two"), response("long", "bytes", "bad"), response("submit")],
            {},
            "completed",
            4,
        ),
        ([response(), response("write"), response("submit")], {}, "completed", 4),
        ([response(malformed="{"), response(), response()], {}, "completed", 4),
        ([response("read")], {"step_limit": 1}, "paused", 2),
        ([response("", "read"), response("submit")], {}, "completed", 3),
        ([response()], {"cost_limit": 0.5}, "paused", 2),
        ([response("submit", "never")], {}, "incomplete", 1),
        ([response("read", "submit", "never")], {}, "incomplete", 1),
        ([response("read", "submit")], {}, "incomplete", 1),
        ([RuntimeError("temporary"), response("submit")], {}, "completed", 2),
    ],
)
def test_parity_with_native_agent(tmp_path, monkeypatch, responses, config, status, nodes):
    store, sandbox, capture, transport, adapter = integrated(
        tmp_path, responses, agent_config=config
    )
    expected_env = Sandbox()
    expected, expected_result, expected_transport = original(
        monkeypatch, responses, adapter, expected_env, config
    )
    before_global_calls = upstream_model.GLOBAL_MODEL_STATS.n_calls
    result = adapter.run(AgentTask("task", "Task"))
    actual = adapter.native_agent
    assert result == expected_result
    assert actual.messages == expected.messages
    assert (actual.cost, actual.n_calls, actual.n_consecutive_format_errors) == (
        expected.cost,
        expected.n_calls,
        expected.n_consecutive_format_errors,
    )
    assert sandbox.calls == expected_env.calls
    assert transport.requests == expected_transport.requests
    assert upstream_model.GLOBAL_MODEL_STATS.n_calls == before_global_calls
    assert adapter.recorder.end.status == status
    assert len(store.list(Node)) == len(capture.calls) == nodes
    assert store.check_integrity().ok
    assert bool(store.check_integrity().incomplete_steps) == (status == "incomplete")
    for node in store.list(Node):
        assert store.get_node(node.node_id) == node
        context = store.read_json(node.context)
        assert context["messages"] == actual.messages[: len(context["messages"])]
    final = store.read_json(adapter.recorder.end.details)
    assert final["final_control_context"]["messages"] == actual.messages
    accounting = final["final_control_context"]["model_accounting"]
    assert accounting["unaccounted_client_entries"] == len(transport.requests) - actual.n_calls
    if accounting["unaccounted_client_entries"]:
        assert accounting["cost_known"] is False
    assert final["record_complete"] == (status != "incomplete")
    record_root = store.root
    store.close()
    with RecordStore.open(record_root) as reopened:
        assert reopened.check_integrity().ok
        assert len(reopened.list(Node)) == nodes


def test_raw_evidence_adoption_and_state_sharing(tmp_path):
    store, _sandbox, capture, transport, agent = integrated(
        tmp_path, [response("write"), response("long", "bytes", "bad"), response("submit")]
    )
    agent.run(AgentTask("task", "Task"))
    nodes = store.list(Node)
    assert nodes[0].state_id != nodes[1].state_id == nodes[2].state_id == nodes[3].state_id
    c0 = store.read_json(nodes[0].context)
    assert [m["role"] for m in c0["messages"]] == ["system", "user"]
    assert c0["counters"]["n_calls"] == 0
    model = read_call(store, nodes[2].model_execution_id)
    assert model.execution.visibility == "opaque-retries"
    assert [e.kind for e in model.evidence] == ["request", "response"]
    assert store.read_json(model.evidence[0].payload) == transport.requests[1]
    tools = [read_call(store, ident) for ident in nodes[2].tool_execution_ids]
    long = store.read_json(tools[0].events[-1].payload)["messages"][0]
    assert "characters elided" in long["content"]
    assert len(long["extra"]["raw_output"]) > 20000
    assert store.read_bytes(tools[1].events[1].payload) == b"a\xff\r\nb\rc"
    assert (
        store.read_json(tools[1].events[-1].payload)["messages"][0]["extra"]["raw_output"]
        == "a\ufffd\nb\nc"
    )
    assert tools[2].events[1].outcome == "error" and tools[2].phase == "accepted"
    before = len(capture.calls)
    agent.native_agent.save(None)
    assert len(capture.calls) == before  # manual saves are not boundaries
    (
        store.root
        / "artifacts"
        / model.evidence[-1].payload.sha256[:2]
        / model.evidence[-1].payload.sha256[2:]
    ).unlink()
    with pytest.raises(IntegrityError):
        store.get_node(nodes[2].node_id)
    assert not store.check_integrity().ok
    store.close()


def test_format_error_counter_snapshot_and_reset(tmp_path):
    store, _, _, _, adapter = integrated(
        tmp_path, [response(), response("read"), response()], agent_config={"step_limit": 3}
    )
    adapter.run(AgentTask("task", "Task"))
    contexts = [store.read_json(n.context) for n in store.list(Node)]
    assert [c["counters"]["consecutive_format_errors"] for c in contexts] == [0, 1, 0, 1]
    assert [c["counters"]["cost"] for c in contexts] == [0, 0.5, 1, 1.5]
    assert not store.list(Node)[1].tool_execution_ids
    assert read_call(store, store.list(Node)[1].model_execution_id).events[1].outcome == "returned"
    store.close()


def test_capture_time_counts_toward_wall_budget_without_changing_snapshot(tmp_path, monkeypatch):
    store, _, capture, transport, adapter = integrated(
        tmp_path, [], agent_config={"wall_time_limit_seconds": 1}
    )
    now = [1700000000.0]
    monkeypatch.setattr(native.time, "time", lambda: now[0])

    def slow_capture(*args):
        now[0] += 2
        return capture(*args)

    adapter._capture_state = slow_capture
    result = adapter.run(AgentTask("task", "Task"))
    assert result["exit_status"] == "TimeExceeded"
    assert not transport.requests and not store.list(Step)
    assert adapter.recorder.end.status == "paused"
    assert store.read_json(store.list(Node)[0].context)["clock"]["elapsed_seconds"] == 0
    store.close()


@pytest.mark.parametrize(
    "fault", ["request", "response", "delivered", "accepted", "capture", "commit"]
)
def test_persistence_failure_never_repeats_paid_request(tmp_path, monkeypatch, fault):
    store, sandbox, capture, transport, adapter = integrated(
        tmp_path, [response("write")], retry_attempts=3
    )
    save, append, commit = store.save, store.append_event, store.commit_node

    def save_fault(fact):
        if isinstance(fact, CallEvidence) and fact.kind == fault:
            raise OSError("injected storage error")
        return save(fact)

    def append_fault(execution_id, kind, **kwargs):
        if kind == fault:
            raise OSError("injected adoption error")
        return append(execution_id, kind, **kwargs)

    def capture_fault(*args):
        if fault == "capture" and capture.calls:
            raise OSError("injected capture error")
        return capture(*args)

    def commit_fault(node):
        if fault == "commit" and node.index:
            raise OSError("injected commit error")
        return commit(node)

    monkeypatch.setattr(store, "save", save_fault)
    monkeypatch.setattr(store, "append_event", append_fault)
    monkeypatch.setattr(store, "commit_node", commit_fault)
    adapter._capture_state = capture_fault
    with pytest.raises(RecordingError):
        adapter.run(AgentTask("task", "Task"))
    assert len(transport.requests) == (0 if fault == "request" else 1)
    assert len(sandbox.calls) == (1 if fault in ("capture", "commit") else 0)
    assert len(store.list(Node)) == 1
    assert store.check_integrity().ok
    assert adapter.recorder.end.status == "failed"
    assert any(
        store.read_json(f.details).get("phase") == "mini-termination" for f in store.list(Failure)
    )
    store.close()


@pytest.mark.parametrize(
    "command, error, end",
    [("timeout", SandboxTimeout, "failed"), ("cancel", KeyboardInterrupt, "cancelled")],
)
def test_unknown_tool_outcome_stops_and_preserves_prefix(tmp_path, command, error, end):
    store, sandbox, _, transport, adapter = integrated(
        tmp_path, [response("write"), response(command, "never")]
    )
    with pytest.raises(error):
        adapter.run(AgentTask("task", "Task"))
    assert len(store.list(Node)) == 2 and len(transport.requests) == 2
    assert len(sandbox.calls) == 2
    assert adapter.recorder.end.status == end
    assert store.check_integrity().ok and len(store.check_integrity().pending_executions) == 1
    assert len(store.check_integrity().incomplete_steps) == 1
    store.close()


def test_unknown_cost_is_not_reported_as_free(tmp_path, monkeypatch):
    store, _, _, _, adapter = integrated(tmp_path, [response("submit")])
    adapter._model.config.cost_tracking = "ignore_errors"
    monkeypatch.setattr(
        upstream_model.litellm.cost_calculator, "completion_cost", lambda *a, **k: 0
    )
    adapter.run(AgentTask("task", "Task"))
    context = store.read_json(store.list(Node)[-1].context)
    assert context["counters"]["cost"] == 0  # native budget counter, not a cost claim
    assert context["model_accounting"]["cost_known"] is False
    assert context["model_accounting"]["calls"] == [{"known": False, "cost": None}]
    store.close()


def test_registry_capabilities_and_public_configuration(tmp_path):
    assert get_agent_class("mini-swe-agent") is MiniSweAgent
    assert not MiniSweAgent.info.capabilities.context_restore
    with pytest.raises(ValueError):
        get_agent_class("unsupported")
    with pytest.raises(ValueError, match="private"):
        native.ObservedModel(model_name="x", model_kwargs={"api_key": "fixture-secret"})
    with pytest.raises(ValueError, match="streaming"):
        native.ObservedModel(model_name="x", model_kwargs={"stream": True})
    with pytest.raises(ValueError, match="interpreter"):
        native.BorrowedEnvironment(Sandbox(), interpreter=["sh", "-c"])


def test_response_serialization_failure_is_not_retried(tmp_path):
    class BrokenResponse:
        def model_dump(self, **kwargs):
            raise ValueError("cannot serialize observed response")

    store, sandbox, _, transport, adapter = integrated(
        tmp_path, [BrokenResponse()], retry_attempts=3
    )
    with pytest.raises(RecordingError):
        adapter.run(AgentTask("task", "Task"))
    assert len(transport.requests) == 1 and not sandbox.calls
    assert len(store.list(Node)) == 1
    assert adapter.recorder.end.status == "failed"
    store.close()


def test_c0_capture_failure_exposes_no_model_request(tmp_path):
    store, _, _, transport, adapter = integrated(tmp_path, [])

    def fail(*args):
        raise OSError("initial capture failed")

    adapter._capture_state = fail
    with pytest.raises(RecordingError):
        adapter.run(AgentTask("task", "Task"))
    assert not transport.requests and not store.list(Node) and not store.list(Step)
    assert adapter.recorder.end.status == "failed"
    store.close()


def test_native_add_messages_partial_write_is_not_repeated(tmp_path, monkeypatch):
    store, sandbox, _, transport, adapter = integrated(tmp_path, [response("one", "two")])
    original_add = DefaultAgent.add_messages
    batches = []

    def partial(agent, *messages):
        batches.append(tuple(m["role"] for m in messages))
        if messages and messages[0]["role"] == "tool":
            original_add(agent, messages[0])
            raise ValueError("native receiver failed after partial write")
        return original_add(agent, *messages)

    monkeypatch.setattr(DefaultAgent, "add_messages", partial)
    with pytest.raises(RecordingError):
        adapter.run(AgentTask("task", "Task"))
    assert batches.count(("tool", "tool")) == 1
    assert len(transport.requests) == 1 and len(sandbox.calls) == 2
    assert len(store.list(Node)) == 1
    assert len([m for m in adapter.native_agent.messages if m["role"] == "tool"]) == 1
    assert store.check_integrity().ok and store.check_integrity().incomplete_steps
    store.close()


def test_fresh_runs_isolate_counters_and_same_object_cannot_run_twice(tmp_path):
    for index in range(2):
        store, _, _, _, adapter = integrated(tmp_path / str(index), [response("submit")])
        adapter.run(AgentTask("task", "Task"))
        context = store.read_json(store.list(Node)[-1].context)
        assert context["counters"]["n_calls"] == 1
        assert context["global_stats"]["n_calls"] == 1
        with pytest.raises(RuntimeError, match="fresh"):
            adapter.run(AgentTask("task", "Task"))
        store.close()


def test_credentials_remain_private_even_in_sdk_exception(tmp_path):
    secret = "test-private-key-1234"
    credentials = LLMConfig(model="fixture", api_key=secret, base_url="https://fixture.invalid/v1")
    store, _, _, _, adapter = integrated(
        tmp_path,
        [RuntimeError(f"SDK failed: {secret} at {credentials.base_url}")],
        credentials=credentials,
        retry_attempts=1,
    )
    with pytest.raises(RuntimeError, match=r"SDK failed: \[private\]"):
        adapter.run(AgentTask("task", "Task"))
    for path in (store.root / "artifacts").rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
            assert credentials.base_url.encode() not in path.read_bytes()
    store.close()


def test_import_does_not_load_global_dotenv_or_change_its_location(tmp_path):
    config = tmp_path / "global"
    config.mkdir()
    (config / ".env").write_text("PALIN_MINI_IMPORT_SENTINEL=loaded\n")
    script = """
import os, socket
def blocked(*a, **k):
    raise AssertionError("unexpected network during import")
socket.socket.connect = blocked
socket.create_connection = blocked
before = os.environ["MSWEA_GLOBAL_CONFIG_DIR"]
from palinreplay.agents.mini import load_native
load_native()
assert "PALIN_MINI_IMPORT_SENTINEL" not in os.environ
assert os.environ["MSWEA_GLOBAL_CONFIG_DIR"] == before
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "MSWEA_GLOBAL_CONFIG_DIR": str(config),
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        },
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("http_status", [200, 500])
def test_real_litellm_and_openai_sdk_path_with_offline_http(tmp_path, monkeypatch, http_status):
    """Mock only HTTP: exercise LiteLLM lazy imports and actual SDK retry settings."""
    import httpx
    from litellm.llms.openai.openai import OpenAIChatCompletion

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        body = (
            response("submit").model_dump(mode="json")
            if http_status == 200
            else {"error": {"message": "fixture server failure", "type": "server_error"}}
        )
        return httpx.Response(http_status, json=body, request=request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            OpenAIChatCompletion, "_get_sync_http_client", staticmethod(lambda: client)
        )
        with RecordStore.create(tmp_path / "record") as store:
            sandbox = Sandbox()
            credentials = LLMConfig(
                model=f"palin-http-{http_status}",
                api_key="fixture-only-key",
                base_url=f"https://fixture-{http_status}.invalid/v1",
                max_retries=0,
            )
            adapter = MiniSweAgent(
                sandbox=sandbox,
                store=store,
                capture_state=Capture(sandbox),
                model_config={
                    "model_name": f"openai/{credentials.model}",
                    "model_kwargs": {"max_tokens": 8192, "num_retries": 0},
                },
                agent_config=AGENT,
                credentials=credentials,
                retry_attempts=1,
            )
            if http_status == 200:
                assert adapter.run(AgentTask("task", "Task"))["exit_status"] == "Submitted"
                assert adapter.recorder.end.status == "completed"
            else:
                with pytest.raises(upstream_model.litellm.InternalServerError):
                    adapter.run(AgentTask("task", "Task"))
                assert adapter.recorder.end.status == "failed"
            assert len(requests) == 1
            assert requests[0]["max_tokens"] == 8192
            assert requests[0]["tools"] == [BASH_TOOL]
            assert not any("extra" in m for m in requests[0]["messages"])


@pytest.mark.parametrize("mismatch", ["version", "commit"])
def test_adapter_rejects_unverified_upstream_installation(monkeypatch, mismatch):
    from palinreplay.agents import mini

    distribution = SimpleNamespace(
        version="wrong" if mismatch == "version" else mini.MINI_VERSION,
        read_text=lambda path: json.dumps(
            {"vcs_info": {"commit_id": "wrong" if mismatch == "commit" else mini.MINI_COMMIT}}
        ),
    )
    monkeypatch.setattr(mini, "_native", None)
    monkeypatch.setattr(mini.importlib.metadata, "distribution", lambda name: distribution)
    with pytest.raises(RuntimeError, match="requires mini-swe-agent"):
        mini.load_native()


def test_ambiguous_tool_ids_cannot_publish_or_execute_a_partial_group(tmp_path):
    raw = response("one", "two")
    raw.choices[0].message.tool_calls[1].id = "tool-0"
    store, sandbox, _, transport, adapter = integrated(tmp_path, [raw])
    with pytest.raises(ValueError, match="duplicate provider tool-call"):
        adapter.run(AgentTask("task", "Task"))
    assert len(transport.requests) == 1 and not sandbox.calls
    assert len(store.list(Node)) == 1 and adapter.recorder.end.status == "failed"
    assert store.check_integrity().ok and store.check_integrity().incomplete_steps
    store.close()
