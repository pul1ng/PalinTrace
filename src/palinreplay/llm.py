"""Direct, non-streaming OpenAI SDK integration; no Agent or Harbor dependency."""

import os
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

import httpx
from openai import OpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

# Project-relative default; override with --config or PALINREPLAY_LLM_CONFIG.
DEFAULT_CONFIG_PATH = Path("config/llm/llm.toml")
CONFIG_PATH_ENV = "PALINREPLAY_LLM_CONFIG"


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = "https://api.openai.com/v1"
    model: str = Field(min_length=1)
    api_key: SecretStr = Field(repr=False)
    timeout: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    # A transport failure may follow a charged/completed request. Do not silently retry it.
    max_retries: int = Field(default=0, ge=0)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "base_url must not contain credentials, query parameters or a fragment"
            )
        return value

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be blank")
        return value.strip()

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("api_key must not be blank")
        return value

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        **overrides: Any,
    ) -> Self:
        """Precedence: explicit overrides > TOML [llm] > environment > defaults.

        With no explicit path, ``PALINREPLAY_LLM_CONFIG`` is consulted, then
        ``config/llm/llm.toml`` relative to the current directory. A missing
        default file is not an error; a missing explicit path is.
        No environment file is automatically loaded. Secrets are masked by Pydantic
        serialization and omitted from repr; do not log the source TOML/environment.
        """
        env = os.environ if environ is None else environ
        values: dict[str, Any] = {
            field: env[name]
            for field, name in {
                "base_url": "OPENAI_BASE_URL",
                "model": "OPENAI_MODEL",
                "api_key": "OPENAI_API_KEY",
            }.items()
            if env.get(name)
        }
        config_path = Path(path) if path is not None else _default_config_path(env)
        if config_path is not None:
            with config_path.open("rb") as handle:
                document = tomllib.load(handle)
            section = document.get("llm")
            if not isinstance(section, dict):
                raise ValueError(f"{config_path} must contain a [llm] table")
            values.update(section)
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls.model_validate(values)


def _default_config_path(env: Mapping[str, str]) -> Path | None:
    configured = env.get(CONFIG_PATH_ENV)
    candidate = Path(configured) if configured else DEFAULT_CONFIG_PATH
    return candidate if candidate.is_file() else None


def mini_model_name(model: str) -> str:
    """Use Record's explicit LiteLLM provider convention for an API model name."""
    return model if "/" in model else f"openai/{model}"


class OpenAIChatClient:
    """Keep the SDK ChatCompletion intact, including tool calls and usage.

    ``complete`` supports normal OpenAI chat-completions parameters without
    inventing provider defaults. ``ask`` is a convenience for text-only calls.
    Streaming and the Responses API are not part of this initial implementation.
    """

    def __init__(self, config: LLMConfig, *, http_client: httpx.Client | None = None):
        self.config = config
        self._client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key.get_secret_value(),
            timeout=config.timeout,
            max_retries=config.max_retries,
            http_client=http_client,
        )

    def complete(
        self, messages: Sequence[ChatCompletionMessageParam], **parameters: Any
    ) -> ChatCompletion:
        if not messages:
            raise ValueError("messages must not be empty")
        if parameters.get("stream"):
            raise ValueError("streaming is not supported; use non-streaming requests")
        if "model" in parameters:
            raise ValueError("set model in LLMConfig, not request parameters")
        parameters.pop("stream", None)
        return self._client.chat.completions.create(
            model=self.config.model, messages=list(messages), stream=False, **parameters
        )

    def ask(self, prompt: str, *, system_prompt: str | None = None, **parameters: Any) -> str:
        messages: list[ChatCompletionMessageParam] = []
        if system_prompt is not None:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        response = self.complete(messages, **parameters)
        if not response.choices or response.choices[0].message.content is None:
            raise ValueError("response has no text content; use complete() for tool calls/refusals")
        return response.choices[0].message.content

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
