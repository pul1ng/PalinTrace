import json

import httpx
import pytest
from openai import APIStatusError, APITimeoutError
from pydantic import ValidationError

from palinreplay.llm import LLMConfig, OpenAIChatClient

SECRET = "unit-test-only-not-a-real-key"


def config(**kwargs):
    return LLMConfig(
        base_url="https://model.invalid/custom/v1", model="sample-model", api_key=SECRET, **kwargs
    )


def response(content="hello", **message_extra):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "sample-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content, **message_extra},
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    }


def test_three_fields_send_real_sdk_request_to_mock_transport():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response("你好"))

    with OpenAIChatClient(
        config(), http_client=httpx.Client(transport=httpx.MockTransport(handler))
    ) as client:
        assert client.ask("问题", system_prompt="system", max_completion_tokens=10) == "你好"
    request = requests[0]
    assert str(request.url) == "https://model.invalid/custom/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    body = json.loads(request.content)
    assert body["model"] == "sample-model"
    assert body["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "问题"},
    ]
    assert body["max_completion_tokens"] == 10
    assert "temperature" not in body
    assert body["stream"] is False


def test_complete_keeps_tool_calls_and_usage():
    tool_calls = [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "shell", "arguments": '{"command":"pwd"}'},
        }
    ]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=response(None, tool_calls=tool_calls))
    )
    with OpenAIChatClient(config(), http_client=httpx.Client(transport=transport)) as client:
        result = client.complete(
            [{"role": "user", "content": "run"}],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "shell", "parameters": {"type": "object"}},
                }
            ],
        )
        assert result.choices[0].message.tool_calls[0].id == "call-1"
        assert result.usage.total_tokens == 6
        assert result.model_dump()["id"] == "chatcmpl-test"


def test_http_failure_is_not_retried():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            500, json={"error": {"message": "server failed", "type": "server_error"}}
        )

    with OpenAIChatClient(
        config(), http_client=httpx.Client(transport=httpx.MockTransport(handler))
    ) as client:
        with pytest.raises(APIStatusError):
            client.ask("hello")
    assert len(seen) == 1


def test_timeout_is_not_retried():
    seen = []

    def handler(request):
        seen.append(request)
        raise httpx.ReadTimeout("mock timeout", request=request)

    with OpenAIChatClient(
        config(), http_client=httpx.Client(transport=httpx.MockTransport(handler))
    ) as client:
        with pytest.raises(APITimeoutError):
            client.ask("hello")
    assert len(seen) == 1


def test_ask_does_not_discard_tool_only_response():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response(None)))
    with OpenAIChatClient(config(), http_client=httpx.Client(transport=transport)) as client:
        with pytest.raises(ValueError, match="no text content"):
            client.ask("hello")


def test_rejects_streaming_empty_messages_and_model_override_before_request():
    transport = httpx.MockTransport(lambda request: pytest.fail("no request expected"))
    with OpenAIChatClient(config(), http_client=httpx.Client(transport=transport)) as client:
        with pytest.raises(ValueError, match="streaming"):
            client.complete([{"role": "user", "content": "hi"}], stream=True)
        with pytest.raises(ValueError, match="empty"):
            client.complete([])
        with pytest.raises(ValueError, match="LLMConfig"):
            client.complete([{"role": "user", "content": "hi"}], model="other")


def test_secret_not_in_repr_or_json():
    cfg = config()
    assert SECRET not in repr(cfg)
    assert SECRET not in cfg.model_dump_json()
    assert cfg.api_key.get_secret_value() == SECRET


def test_config_file_environment_and_overrides(tmp_path):
    path = tmp_path / "llm.toml"
    path.write_text('[llm]\nmodel = "file-model"\nbase_url = "https://file.invalid/v1"\n')
    env = {
        "OPENAI_API_KEY": SECRET,
        "OPENAI_MODEL": "env-model",
        "OPENAI_BASE_URL": "https://env.invalid/v1",
    }
    cfg = LLMConfig.load(path, environ=env, model="explicit-model")
    assert cfg.model == "explicit-model"
    assert cfg.base_url == "https://file.invalid/v1"
    assert cfg.max_retries == 0
    assert cfg.api_key.get_secret_value() == SECRET


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": " "},
        {"model": " "},
        {"base_url": "not-url"},
        {"base_url": "https://user:secret@example.invalid/v1"},
        {"base_url": "https://example.invalid/v1?key=secret"},
        {"timeout": 0},
        {"max_retries": -1},
    ],
)
def test_invalid_configuration(overrides):
    values = {"base_url": "https://example.invalid/v1", "model": "m", "api_key": SECRET}
    with pytest.raises(ValidationError):
        LLMConfig(**(values | overrides))


def test_missing_configuration_and_wrong_file_section(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError):
        LLMConfig.load(environ={})
    path = tmp_path / "wrong.toml"
    path.write_text('[other]\nmodel = "x"\n')
    with pytest.raises(ValueError, match=r"\[llm\]"):
        LLMConfig.load(path, environ={})


def test_default_config_file_is_discovered_in_cwd(tmp_path, monkeypatch):
    target = tmp_path / "config" / "llm" / "llm.toml"
    target.parent.mkdir(parents=True)
    target.write_text('[llm]\nmodel = "default-file-model"\nbase_url = "https://file.invalid/v1"\n')
    monkeypatch.chdir(tmp_path)
    cfg = LLMConfig.load(environ={"OPENAI_API_KEY": SECRET})
    assert cfg.model == "default-file-model"
    assert cfg.base_url == "https://file.invalid/v1"


def test_missing_default_config_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = LLMConfig.load(environ={"OPENAI_API_KEY": SECRET, "OPENAI_MODEL": "env-model"})
    assert cfg.model == "env-model"


def test_config_path_environment_variable(tmp_path, monkeypatch):
    target = tmp_path / "custom.toml"
    target.write_text('[llm]\nmodel = "env-path-model"\n')
    monkeypatch.chdir(tmp_path)
    cfg = LLMConfig.load(environ={"OPENAI_API_KEY": SECRET, "PALINREPLAY_LLM_CONFIG": str(target)})
    assert cfg.model == "env-path-model"


def test_explicit_missing_config_path_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        LLMConfig.load(tmp_path / "absent.toml", environ={"OPENAI_API_KEY": SECRET})
