import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from palinreplay.resume.context import ResumePrefix
from palinreplay.resume.decision import (
    build_resume_messages,
    prepare_resume_decision,
    resolve_resume_model,
)


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
    return SimpleNamespace(
        prefix=ResumePrefix(materials),
        start=SimpleNamespace(model="M", max_steps=2, source_store=tmp_path / "source",
                              node=SimpleNamespace(run_id="parent-run", node_id="source-C20",
                                                   index=20, state_id="state-C20")),
        report={"restore_status": "passed", "preparation": {"status": "ready"}},
        report_path=tmp_path / "report.json", executor=Mock(),
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
    with pytest.raises(OSError, match="evidence unavailable"):
        prepare_resume_decision(session, llm_config=llm_config)
    assert session.report["decision"]["model_requests"] == 0
    assert not session.executor.mock_calls
