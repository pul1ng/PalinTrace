import os
from pathlib import Path

import pytest

from palinreplay.core.errors import RecordUsageError
from palinreplay.preprocess.config import (
    DEFAULT_CONFIG_PATH,
    EXAMPLE_CONFIG_PATH,
    load_preprocess_config,
)


def write_config(path: Path, *, model="fixture-model", api_key="fixture-key", extra="") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = f'api_key = "{api_key}"\n' if api_key is not None else ""
    path.write_text(
        "[llm]\n"
        'base_url = "https://api.example.test/v1"\n'
        f'model = "{model}"\n'
        f"{key}"
        "timeout = 10.0\n"
        "max_retries = 0\n"
        "\n[preprocess]\n"
        "request_token_budget = 2000\n"
        "response_token_reserve = 200\n"
        "concurrency = 2\n"
        f"{extra}"
    )
    return path


def test_example_template_is_tracked_and_real_path_is_ignored():
    root = Path(__file__).resolve().parents[2]
    assert (root / EXAMPLE_CONFIG_PATH).is_file()
    ignored = __import__("subprocess").run(
        ["git", "check-ignore", "-q", "config/llm/preprocess_llm.toml"], cwd=root
    )
    assert ignored.returncode == 0
    tracked = __import__("subprocess").run(
        ["git", "check-ignore", "-q", "config/llm/preprocess_llm.toml.example"], cwd=root
    )
    assert tracked.returncode == 1


def test_explicit_config_is_loaded_with_preprocess_settings(tmp_path):
    path = write_config(tmp_path / "preprocess_llm.toml")
    config, selected = load_preprocess_config(path)
    assert selected == path
    assert config.llm.model == "fixture-model"
    assert config.preprocess.request_token_budget == 2000
    assert config.preprocess.response_token_reserve == 200
    assert config.preprocess.concurrency == 2
    assert "fixture-key" not in repr(config)


def test_default_missing_file_does_not_fall_back_to_recording_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recording = write_config(tmp_path / "config/llm/llm.toml")
    fallback = write_config(tmp_path / "fallback.toml")
    monkeypatch.setenv("PALINREPLAY_LLM_CONFIG", str(fallback))
    with pytest.raises(RecordUsageError, match="preprocess"):
        load_preprocess_config()
    assert recording.is_file() and fallback.is_file()


def test_explicit_missing_file_fails_even_when_recording_config_exists(tmp_path):
    write_config(tmp_path / "config/llm/llm.toml")
    with pytest.raises(RecordUsageError, match="not found"):
        load_preprocess_config(tmp_path / "missing.toml")


def test_api_key_can_come_from_environment_without_reading_recording_config(tmp_path):
    path = write_config(tmp_path / "preprocess_llm.toml", api_key=None)
    config, _ = load_preprocess_config(
        path, environ={"OPENAI_API_KEY": "environment-key", "PALINREPLAY_LLM_CONFIG": "ignored"}
    )
    assert config.llm.api_key.get_secret_value() == "environment-key"


def test_missing_api_key_is_an_input_error(tmp_path):
    path = write_config(tmp_path / "preprocess_llm.toml", api_key=None)
    with pytest.raises(RecordUsageError, match="api_key"):
        load_preprocess_config(path, environ={})


def test_unknown_tables_and_tokenizer_conflicts_are_rejected(tmp_path):
    path = write_config(tmp_path / "preprocess_llm.toml")
    path.write_text(path.read_text() + "\n[recording]\nmodel = 'wrong'\n")
    with pytest.raises(RecordUsageError, match="unknown"):
        load_preprocess_config(path)

    path = write_config(tmp_path / "conflict.toml")
    path.write_text(
        path.read_text()
        + '\ntokenizer_model = "same"\ntokenizer_encoding = "cl100k_base"\n'
    )
    with pytest.raises(RecordUsageError, match="only one"):
        load_preprocess_config(path)


def test_default_path_is_the_preprocess_template_target():
    assert DEFAULT_CONFIG_PATH == Path("config/llm/preprocess_llm.toml")
    assert os.path.basename(EXAMPLE_CONFIG_PATH) == "preprocess_llm.toml.example"
