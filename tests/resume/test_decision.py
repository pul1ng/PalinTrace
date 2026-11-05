import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from palinreplay.core.errors import RecordUsageError
from palinreplay.resume import (
    ResumeDecisionError,
    ResumeSession,
    decide_resume_start,
    restore_resume_start,
)
from palinreplay.resume.context import ResumePrefix
from palinreplay.resume.decision import (
    build_resume_messages,
    prepare_resume_decision,
    resolve_resume_model,
)
from tests.resume import test_services, test_session

setup = test_session.setup
prepared = test_services.prepared


@pytest.fixture
def session(tmp_path):
    messages = [{"role": "system", "content": "Use bash"}, {"role": "user", "content": "Task"}]
    for index in range(1, 21):
        call_id = f"service-tool-{index}"
        messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": call_id, "type": "function", "function": {
                    "name": "bash", "arguments": '{"command":"pwd"}',
                },
            }], "extra": {"actions": [{"tool_call_id": call_id, "command": "pwd"}]}},
            {"role": "tool", "tool_call_id": call_id, "content": f"result {index}"},
        ])
    materials = SimpleNamespace(
        context={"messages": messages, "counters": {"n_calls": 20, "cost": 0}},
        input_position={"next_message": 42},
    )
    materials.context["config"] = {"model": {
        "model_name": "parent-A", "model_kwargs": {"max_tokens": 128},
    }}
    return ResumeSession(
        prefix=ResumePrefix(materials),
        start=SimpleNamespace(model="M", max_steps=2, source_store=tmp_path / "source",
                              node=SimpleNamespace(run_id="parent-run", node_id="source-C20",
                                                   index=20, state_id="state-C20")),
        report={"phase": "caller", "restore_status": "passed", "preparation": {"status": "ready"}},
        materials=materials, restored=SimpleNamespace(), output=tmp_path, executor=Mock(state="running"),
    )


def test_inherited_messages_are_an_independent_copy(session):
    saved = deepcopy(session.prefix.context)
    messages = build_resume_messages(session)
    assert len(messages) == 42 and messages == saved["messages"]
    assert messages[-1]["role"] == "tool"
    assert messages[-1]["tool_call_id"] == "service-tool-20"
    messages[2]["tool_calls"][0]["function"]["arguments"] = '{"command":"changed"}'
    messages[2]["extra"]["actions"][0]["command"] = "changed"
    messages[-1]["content"] = "changed"
    messages.append({"role": "user", "content": "branch only"})
    assert session.prefix.context == saved
    assert session.prefix.input_position == {"next_message": 42}


@pytest.mark.parametrize("instruction", [None, "下一步先检查 B 的日志", "  keep spacing\n"])
def test_optional_instruction_is_appended_once(session, instruction):
    saved = deepcopy(session.prefix.messages)
    for _ in range(2):
        messages = build_resume_messages(session, instruction=instruction)
        assert messages[:42] == saved
        assert len(messages) == 42 + (instruction is not None)
        if instruction is not None:
            assert messages[42] == {"role": "user", "content": instruction}
    assert session.prefix.messages == saved
    assert session.prefix.input_position["next_message"] == 42


@pytest.mark.parametrize("instruction", ["", " \n", 1])
def test_instruction_requires_text(session, instruction):
    with pytest.raises(ValueError, match="instruction"):
        build_resume_messages(session, instruction=instruction)


@pytest.fixture
def llm_config(tmp_path):
    path = tmp_path / "llm.toml"
    path.write_text('[llm]\nmodel="configured-A"\napi_key="decision-test-secret"\n'
                    'base_url="https://model.invalid/v1"\nmax_retries=3\n')
    return path


@pytest.mark.parametrize("selected,expected", [("M", "openai/M"), ("openai/M", "openai/M")])
def test_branch_model_overrides_configured_and_parent_model(session, llm_config, selected, expected):
    session.start.model = selected
    session.prefix.context["config"] = {"model": {"model_name": "parent-A"}}
    resolved = resolve_resume_model(session, llm_config=llm_config)
    assert resolved.model == expected and resolved.max_retries == 0
    assert resolved.api_key.get_secret_value() == "decision-test-secret"
    assert session.prefix.config["model"]["model_name"] == "parent-A"
    assert "decision-test-secret" not in repr(resolved)


def test_missing_credentials_fail_before_any_transport(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("PALINREPLAY_LLM_CONFIG", "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="configuration invalid: api_key"):
        resolve_resume_model(session)


def test_invalid_configuration_does_not_print_private_inputs(session, llm_config):
    llm_config.write_text('[llm]\nmodel="A"\napi_key="decision-test-secret"\n'
                          'base_url="bad-url-with-private-value"\n')
    with pytest.raises(ValueError, match="base_url") as raised:
        resolve_resume_model(session, llm_config=llm_config)
    assert "decision-test-secret" not in str(raised.value)
    assert "bad-url-with-private-value" not in str(raised.value)


@pytest.mark.parametrize("instruction", [None, "下一步先检查 B 的日志"])
def test_prepared_input_is_durable_and_public(session, llm_config, instruction):
    from palinreplay.agents.mini import load_native

    saved = deepcopy(session.prefix.context)
    prepared = prepare_resume_decision(session, instruction=instruction, llm_config=llm_config)

    def capture(**request):
        evidence = json.loads(session.report_path.read_text())["decision"]
        assert evidence["status"] == "prepared" and evidence["model_requests"] == 0
        assert evidence["request"] == request
        assert evidence["source"]["node_id"] == "source-C20"
        assert evidence["messages"] == prepared.messages
        assert evidence["inherited_message_count"] == 42
        assert evidence["instruction"] == instruction
        assert request["model"] == "openai/M"
        assert request["tools"] == [load_native().BASH_TOOL]
        assert len(request["messages"]) == 42 + (instruction is not None)
        assert not any("extra" in message for message in request["messages"])
        assert request["messages"][41]["tool_call_id"] == "service-tool-20"
        if instruction is not None:
            assert request["messages"][-1] == {"role": "user", "content": instruction}
        assert "decision-test-secret" not in session.report_path.read_text()
        assert "api_key" not in request and "api_base" not in request

    capture(**prepared.request)
    assert session.prefix.context == saved
    assert not session.executor.mock_calls


def test_failed_evidence_write_never_delivers_prepared_request(session, llm_config, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("evidence unavailable")

    monkeypatch.setattr("palinreplay.resume.decision.write_json", fail)
    with pytest.raises(ResumeDecisionError, match="evidence unavailable") as raised:
        prepare_resume_decision(session, llm_config=llm_config)
    assert raised.value.category == "evidence"
    assert session.report["decision"]["model_requests"] == 0
    assert not session.executor.mock_calls


@pytest.fixture
def response():
    from litellm import ModelResponse

    return ModelResponse(
        id="resume-response", model="M", created=1,
        choices=[{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "A new branch decision", "tool_calls": [{
                "id": "branch-tool-1", "type": "function", "function": {
                    "name": "bash", "arguments": '{"command":"cat /tmp/B.log"}',
                },
            }],
        }}], usage={"prompt_tokens": 120, "completion_tokens": 12, "total_tokens": 132},
    )


@pytest.mark.parametrize("instruction", [None, "下一步先检查 B 的日志"])
def test_decision_queries_once_after_evidence_and_does_not_execute(
    session, llm_config, response, monkeypatch, instruction,
):
    from palinreplay.agents.mini import load_native

    native = load_native()
    saved = deepcopy(session.prefix.context)

    def transport(**request):
        evidence = json.loads(session.report_path.read_text())["decision"]
        assert evidence["status"] == "requesting" and evidence["model_requests"] == 1
        assert evidence["remaining_steps"] == session.start.max_steps - 1
        public = {k: v for k, v in request.items() if k not in ("api_key", "api_base")}
        assert evidence["request"] == public
        assert request["api_key"] == "decision-test-secret"
        assert request["model"] == "openai/M"
        return response

    complete = Mock(side_effect=transport)
    monkeypatch.setattr(native.litellm, "completion", complete)
    monkeypatch.setattr(native.Recorder, "begin_step", Mock(side_effect=AssertionError("step")))
    result = decide_resume_start(session, instruction=instruction, llm_config=llm_config)
    assert complete.call_count == 1
    assert result.raw_response == response.model_dump(mode="json")
    assert result.assistant_message["content"] == "A new branch decision"
    assert len(result.messages) == 42 + (instruction is not None)
    assert session.report["decision"]["model_requests"] == 1
    assert session.report["decision"]["complete_nodes"] == 0
    assert session.prefix.context == saved and not session.executor.mock_calls


def test_decision_real_sdk_sends_one_offline_http_request(
    session, llm_config, response, monkeypatch,
):
    import httpx
    from litellm.llms.openai.openai import OpenAIChatCompletion

    requests = []
    instruction = "下一步先检查 B 的日志"
    session.prefix.config["model"]["model_kwargs"].update(
        temperature=0.25, extra_body={"metadata": {"branch": "resume"}},
    )

    def respond(request):
        body = json.loads(request.content)
        evidence = json.loads(session.report_path.read_text())["decision"]
        # LiteLLM omits null content for assistant tool-call messages on HTTP.
        expected_messages = deepcopy(evidence["request"]["messages"])
        for message in expected_messages:
            if message.get("content", "") is None:
                message.pop("content")
        assert body["messages"] == expected_messages
        assert len(body["messages"]) == 43
        assert body["messages"][-1] == {"role": "user", "content": instruction}
        assert body["tools"] == evidence["request"]["tools"]
        assert body["model"] == "M"
        assert body["temperature"] == 0.25 and body["metadata"] == {"branch": "resume"}
        requests.append(body)
        return httpx.Response(200, json=response.model_dump(mode="json"), request=request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(OpenAIChatCompletion, "_get_sync_http_client",
                            staticmethod(lambda: client))
        result = decide_resume_start(session, instruction=instruction, llm_config=llm_config)
    assert len(requests) == 1 and result.raw_response["id"] == "resume-response"
    assert not session.executor.mock_calls


def test_multiple_actions_preserve_response_order_ids_and_full_arguments(
    session, llm_config, response, monkeypatch,
):
    calls = response.choices[0].message.tool_calls
    second = calls[0].model_copy(deep=True)
    second.id = "branch-tool-2"
    second.function.arguments = '{"command":"tail -20 /tmp/B.log","label":"preserved"}'
    calls.append(second)
    transport = Mock(return_value=response)
    monkeypatch.setattr("palinreplay.resume.decision.complete_decision_request", transport)
    result = decide_resume_start(session, llm_config=llm_config)
    assert result.actions == [
        {"command": "cat /tmp/B.log", "tool_call_id": "branch-tool-1", "name": "bash",
         "arguments": {"command": "cat /tmp/B.log"}},
        {"command": "tail -20 /tmp/B.log", "tool_call_id": "branch-tool-2", "name": "bash",
         "arguments": {"command": "tail -20 /tmp/B.log", "label": "preserved"}},
    ]
    assert result.assistant_message["tool_calls"] == result.raw_response["choices"][0]["message"][
        "tool_calls"]
    assert transport.call_count == 1 and not session.executor.mock_calls
    assert session.report["decision"]["actions"] == result.actions
    assert session.report["decision"]["complete_nodes"] == 0


@pytest.mark.parametrize("fault", ["json", "duplicate_id", "command_type", "no_tools"])
def test_invalid_group_delivers_no_partial_actions(session, llm_config, response, monkeypatch, fault):
    calls = response.choices[0].message.tool_calls
    second = calls[0].model_copy(deep=True)
    second.id = "branch-tool-2"
    calls.append(second)
    if fault == "json":
        second.function.arguments = "not json"
    elif fault == "duplicate_id":
        second.id = calls[0].id
    elif fault == "command_type":
        second.function.arguments = '{"command":123}'
    else:
        response.choices[0].message.tool_calls = []
    transport = Mock(return_value=response)
    monkeypatch.setattr("palinreplay.resume.decision.complete_decision_request", transport)
    with pytest.raises(ResumeDecisionError) as raised:
        decide_resume_start(session, llm_config=llm_config)
    assert raised.value.category == "tool_format"
    evidence = json.loads(session.report_path.read_text())["decision"]
    assert evidence["raw_response"] == response.model_dump(mode="json")
    assert evidence["usage"] == response.model_dump(mode="json")["usage"]
    assert not evidence.get("actions")
    assert transport.call_count == 1 and not session.executor.mock_calls


@pytest.mark.parametrize("instruction", [None, "下一步先检查 B 的日志"])
@pytest.mark.parametrize("known_cost", [None, 0.25])
def test_new_budget_and_usage_are_independent_of_inherited_history(
    session, llm_config, response, monkeypatch, instruction, known_cost,
):
    from palinreplay.agents.mini import load_native

    history = deepcopy(session.prefix.counters)
    native = load_native()
    if known_cost is not None:
        monkeypatch.setattr(native.LitellmModel, "_calculate_cost",
                            lambda *args: {"cost": known_cost})
    transport = Mock(return_value=response)
    monkeypatch.setattr("palinreplay.resume.decision.complete_decision_request", transport)
    result = decide_resume_start(session, instruction=instruction, llm_config=llm_config)
    accounting = result.accounting
    assert accounting["inherited_counters"] == history
    assert history["n_calls"] == 20 and session.prefix.counters == history
    assert accounting["max_steps"] == 2 and accounting["remaining_steps"] == 1
    assert accounting["model_requests"] == accounting["decisions"] == transport.call_count == 1
    assert accounting["usage"] == response.model_dump(mode="json")["usage"]
    assert accounting["cost"] == known_cost
    assert accounting["cost_known"] == (known_cost is not None)


@pytest.mark.parametrize("category", ["config", "request", "tool_format"])
def test_decision_failures_preserve_preparation_evidence_and_cleanup(
    prepared, session, llm_config, response, monkeypatch, category,
):
    from palinreplay.agents.mini import load_native
    restored = prepared.session
    restored.materials.context.update(deepcopy(session.prefix.context))
    restored.materials.input_position.update(next_message=42)
    transport = Mock(return_value=response)
    if category == "config":
        llm_config.write_text('[llm]\nmodel="M"\napi_key=""\n')
    elif category == "request":
        error = RuntimeError("request failed with decision-test-secret at https://model.invalid/v1")
        error.add_note("private note decision-test-secret")
        transport.side_effect = error
    else:
        response.choices[0].message.tool_calls[0].function.arguments = "invalid json"
    monkeypatch.setattr(load_native().litellm, "completion", transport)
    with pytest.raises(ResumeDecisionError) as raised:
        with restore_resume_start(restored.start, restored.output,
                                  services_config=prepared.path) as live:
            assert live.report["preparation"]["status"] == "ready"
            before = restored.executor.exec.call_count
            decide_resume_start(live, llm_config=llm_config)
    error = raised.value
    report = json.loads((restored.output / "report.json").read_text())
    decision = report["decision"]
    assert error.category == category and error.decision == decision
    assert decision["error"]["category"] == category
    assert decision["status"] == "failed" and decision["actions"] == []
    assert decision["model_requests"] == transport.call_count == (category != "config")
    assert decision["remaining_steps"] == 2 - transport.call_count
    assert decision["tool_executions"] == decision["complete_nodes"] == 0
    assert restored.executor.exec.call_count == before
    assert report["restore_status"] == report["r1"]["status"] == "passed"
    assert report["preparation"]["status"] == "ready"
    assert report["cleanup_confirmed"] and not report["cleanup_errors"]
    assert error.resume_report == report
    restored.executor.close.assert_called_once()
    restored.restored.close.assert_called_once()
    if category == "tool_format":
        assert decision["raw_response"] == response.model_dump(mode="json")
        assert decision["usage"] == response.model_dump(mode="json")["usage"]
    public = json.dumps(report) + str(error) + str(getattr(error, "__notes__", []))
    assert "decision-test-secret" not in public
    assert "https://model.invalid/v1" not in public


def test_http_failure_is_not_retried(session, llm_config, monkeypatch):
    import httpx
    from litellm.llms.openai.openai import OpenAIChatCompletion

    # A distinct endpoint avoids reusing the successful test's cached SDK client.
    llm_config.write_text('[llm]\nmodel="M"\napi_key="decision-test-secret"\n'
                          'base_url="https://failure.invalid/v1"\n')
    requests = []

    def respond(request):
        requests.append(request)
        assert json.loads(session.report_path.read_text())["decision"]["request"]
        return httpx.Response(500, json={"error": {"message": "controlled failure"}}, request=request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(OpenAIChatCompletion, "_get_sync_http_client",
                            staticmethod(lambda: client))
        with pytest.raises(ResumeDecisionError) as raised:
            decide_resume_start(session, llm_config=llm_config)
    assert len(requests) == 1 and raised.value.category == "request"
    assert raised.value.decision["error"]["status_code"] == 500
    assert not session.executor.mock_calls


@pytest.mark.parametrize("key", ["model", "messages", "tools"])
@pytest.mark.parametrize("nested", [False, True])
def test_parent_options_cannot_replace_branch_request(session, llm_config, monkeypatch, key, nested):
    transport = Mock()
    monkeypatch.setattr("palinreplay.resume.decision.complete_decision_request", transport)
    override = {key: "parent replacement"}
    options = session.prefix.config["model"]["model_kwargs"]
    options.update({"extra_body": override} if nested else override)
    with pytest.raises(ResumeDecisionError) as raised:
        decide_resume_start(session, instruction="check B", llm_config=llm_config)
    location = f"model_kwargs.{'extra_body.' if nested else ''}{key}"
    assert raised.value.category == "config" and location in str(raised.value)
    decision = json.loads(session.report_path.read_text())["decision"]
    assert decision["status"] == "failed" and decision["model_requests"] == 0
    assert decision["messages"][-1] == {"role": "user", "content": "check B"}
    transport.assert_not_called()


def test_overlapping_first_decisions_share_one_attempt(session, llm_config, response, monkeypatch):
    session.start.max_steps = 1
    entered, release = Event(), Event()

    def respond(*args):
        entered.set()
        assert release.wait(10)
        return response

    transport = Mock(side_effect=respond)
    monkeypatch.setattr("palinreplay.resume.decision.complete_decision_request", transport)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(decide_resume_start, session, instruction="keep me", llm_config=llm_config)
        try:
            assert entered.wait(10)
            second = pool.submit(decide_resume_start, session, instruction="overwrite",
                                 llm_config=llm_config)
            with pytest.raises(RecordUsageError, match="already attempted"):
                second.result(timeout=5)
        finally:
            release.set()
        assert first.result(timeout=10).accounting["remaining_steps"] == 0
    saved = session.report_path.read_bytes()
    with pytest.raises(RecordUsageError, match="already attempted"):
        decide_resume_start(session, instruction="another", llm_config=llm_config)
    assert session.report_path.read_bytes() == saved
    assert json.loads(saved)["decision"]["instruction"] == "keep me"
    transport.assert_called_once()


def test_closed_unused_session_rejects_first_decision(setup, monkeypatch):
    resolve = Mock()
    transport = Mock()
    monkeypatch.setattr("palinreplay.resume.decision.resolve_resume_model", resolve)
    monkeypatch.setattr("palinreplay.resume.decision.complete_decision_request", transport)
    with restore_resume_start(setup.start, setup.output) as live:
        pass
    saved = live.report_path.read_bytes()
    assert json.loads(saved)["status"] == "passed" and "decision" not in json.loads(saved)
    with pytest.raises(RecordUsageError, match="active session"):
        decide_resume_start(live)
    resolve.assert_not_called()
    transport.assert_not_called()
    assert live.report_path.read_bytes() == saved


@pytest.mark.parametrize("state", ["closed", "failed"])
def test_inactive_executor_cannot_send_a_decision(session, monkeypatch, state):
    session.executor.state = state
    transport = Mock()
    monkeypatch.setattr("palinreplay.resume.decision.complete_decision_request", transport)
    with pytest.raises(RecordUsageError, match="active session"):
        decide_resume_start(session)
    assert "decision" not in session.report
    transport.assert_not_called()


@pytest.mark.parametrize("fault,status", [(KeyboardInterrupt, "interrupted"), (TimeoutError, "failed")])
def test_caught_request_failure_remains_terminal(
    prepared, session, llm_config, monkeypatch, fault, status,
):
    setup = prepared.session
    setup.materials.context.update(deepcopy(session.prefix.context))
    error = fault("request interrupted")
    transport = Mock(side_effect=error)
    monkeypatch.setattr("palinreplay.resume.decision.complete_decision_request", transport)
    with restore_resume_start(setup.start, setup.output, services_config=prepared.path) as live:
        expected_error = KeyboardInterrupt if fault is KeyboardInterrupt else ResumeDecisionError
        with pytest.raises(expected_error) as caught:
            decide_resume_start(live, llm_config=llm_config)
        if fault is KeyboardInterrupt:
            assert caught.value is error
        saved = json.loads(live.report_path.read_text())["decision"]
        assert saved["status"] == status and saved["error"]["error_type"] == fault.__name__
        assert saved["model_requests"] == 1 and saved["remaining_steps"] == 1
        assert saved["decisions"] == 0 and saved["actions"] == []
        with pytest.raises(RecordUsageError, match="already attempted"):
            decide_resume_start(live, llm_config=llm_config)
    final = json.loads(live.report_path.read_text())
    assert final["decision"] == saved and final["status"] == "failed"
    assert final["caller"]["status"] == "completed" and final["cleanup_confirmed"]
    assert final["restore_status"] == "passed" and final["preparation"]["status"] == "ready"
    transport.assert_called_once()


@pytest.mark.parametrize("at_status", ["requesting", "returned", "succeeded"])
def test_decision_publication_failure_preserves_actual_attempts(
    session, llm_config, response, monkeypatch, at_status,
):
    from palinreplay.storage.io import write_json

    def fail_at(path, report, **kwargs):
        if report["decision"]["status"] == at_status:
            raise OSError("journal unavailable")
        write_json(path, report, **kwargs)

    transport = Mock(return_value=response)
    monkeypatch.setattr("palinreplay.resume.decision.complete_decision_request", transport)
    monkeypatch.setattr("palinreplay.resume.decision.write_json", fail_at)
    with pytest.raises(ResumeDecisionError) as caught:
        decide_resume_start(session, llm_config=llm_config)
    assert caught.value.category == "evidence"
    decision = json.loads(session.report_path.read_text())["decision"]
    assert decision["model_requests"] == transport.call_count == int(at_status != "requesting")
    assert decision["remaining_steps"] == session.start.max_steps - transport.call_count
    assert decision["status"] == "failed" and decision["decisions"] == 0 and not decision["actions"]
    if transport.call_count:
        assert decision["raw_response"] == response.model_dump(mode="json")
