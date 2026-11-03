"""Dedicated preprocessing model configuration; never reuse recording settings."""

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from pydantic import Field, StrictInt, model_validator

from palinreplay.core.errors import RecordUsageError
from palinreplay.core.schema import Spec
from palinreplay.llm import LLMConfig

DEFAULT_CONFIG_PATH = Path("config/llm/preprocess_llm.toml")
EXAMPLE_CONFIG_PATH = Path("config/llm/preprocess_llm.toml.example")
CONFIG_PATH_ENV = "PALINREPLAY_PREPROCESS_LLM_CONFIG"


class PreprocessSettings(Spec):
    # The request budget includes the fixed prompt, source, complete calls,
    # chat framing and the reserved response space.
    request_token_budget: StrictInt = Field(default=32768, ge=1024)
    response_token_reserve: StrictInt = Field(default=4096, ge=1)
    concurrency: StrictInt = Field(default=4, ge=1, le=32)
    # Custom OpenAI-compatible aliases do not have a tiktoken mapping. Require an
    # explicit tokenizer instead of silently falling back to bytes/characters.
    tokenizer_model: str | None = None
    tokenizer_encoding: str | None = None
    preparation_timeout_sec: float = Field(default=30.0, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_budget(self) -> Self:
        if self.response_token_reserve >= self.request_token_budget:
            raise ValueError("response_token_reserve must be smaller than request_token_budget")
        if self.tokenizer_model is not None and self.tokenizer_encoding is not None:
            raise ValueError("set only one of tokenizer_model or tokenizer_encoding")
        for value in (self.tokenizer_model, self.tokenizer_encoding):
            if value is not None and not value.strip():
                raise ValueError("tokenizer settings must be nonempty when present")
        return self


class PreprocessConfig(Spec):
    llm: LLMConfig
    preprocess: PreprocessSettings = Field(default_factory=PreprocessSettings)


def load_preprocess_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    **overrides: Any,
) -> tuple[PreprocessConfig, Path]:
    """Load only the dedicated preprocessing TOML.

    Unlike ``LLMConfig.load``, this function never consults the recording
    configuration path, ``PALINREPLAY_LLM_CONFIG`` or a shared default file.
    An API key omitted from the TOML may come from ``OPENAI_API_KEY``; all other
    connection and batching values must be explicit in the preprocessing file.
    """
    env = os.environ if environ is None else environ
    selected = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not selected.is_file():
        raise RecordUsageError(
            f"preprocessing LLM config not found: {selected}; copy {EXAMPLE_CONFIG_PATH} "
            f"and fill in a model and credentials"
        )
    try:
        document = tomllib.loads(selected.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise RecordUsageError(f"cannot read preprocessing LLM config {selected}: {error}") from error
    unknown = sorted(set(document) - {"llm", "preprocess"})
    if unknown:
        raise RecordUsageError(
            f"preprocessing LLM config has unknown tables: {', '.join(unknown)}"
        )
    llm_values = document.get("llm")
    if not isinstance(llm_values, dict):
        raise RecordUsageError(f"{selected} must contain a [llm] table")
    values = dict(llm_values)
    if not values.get("api_key") and env.get("OPENAI_API_KEY"):
        values["api_key"] = env["OPENAI_API_KEY"]
    values.update({key: value for key, value in overrides.items() if value is not None})
    try:
        config = PreprocessConfig.model_validate(
            {"llm": values, "preprocess": document.get("preprocess", {})}
        )
    except ValueError as error:
        raise RecordUsageError(f"invalid preprocessing LLM config {selected}: {error}") from error
    return config, selected
