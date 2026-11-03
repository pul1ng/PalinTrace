from pathlib import Path
from types import SimpleNamespace

import pytest

from palinreplay.cli import main


def test_preprocess_help_exposes_dedicated_config_and_range_free_interface(capsys):
    with pytest.raises(SystemExit) as exit:
        main(["preprocess", "--help"])
    assert exit.value.code == 0
    output = capsys.readouterr().out
    for option in ("--run", "--llm-config", "--output"):
        assert option in output
    assert "--from" not in output and "--to" not in output


def test_missing_dedicated_config_fails_before_opening_source(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    import palinreplay.cli.preprocess as cli

    def forbidden(path):
        pytest.fail("source must not be opened without a configuration")

    monkeypatch.setattr(cli, "store_path", forbidden)
    assert main(["preprocess", str(tmp_path / "record")]) == 1
    assert "preprocessing LLM config not found" in capsys.readouterr().err


def test_cli_passes_explicit_config_and_reports_manifest(tmp_path, monkeypatch, capsys):
    import palinreplay.cli.preprocess as cli

    seen = []

    class Config:
        llm = SimpleNamespace(model="fixture-model")

    manifest = SimpleNamespace(
        services=[SimpleNamespace(), SimpleNamespace()],
        unknowns=[SimpleNamespace()],
    )
    result = SimpleNamespace(manifest=manifest, output=Path("out"))

    def run(source, **kwargs):
        seen.append((source, kwargs))
        return result

    monkeypatch.setattr(cli, "store_path", lambda path: tmp_path / "record")
    monkeypatch.setattr(cli, "record_name", lambda path: "task")
    monkeypatch.setattr(
        "palinreplay.preprocess.config.load_preprocess_config",
        lambda path: (Config(), Path("config/llm/preprocess_llm.toml")),
    )
    monkeypatch.setattr("palinreplay.preprocess.pipeline.run_preprocess", run)
    assert main([
        "preprocess", str(tmp_path), "--run", "run-id", "--llm-config", "custom.toml",
        "--output", "out",
    ]) == 0
    assert seen[0][0] == tmp_path / "record"
    assert seen[0][1]["run_id"] == "run-id"
    assert seen[0][1]["output"] == Path("out")
    assert isinstance(seen[0][1]["config"], Config)
    assert "2 service intervals" in capsys.readouterr().out


def test_cli_cancellation_and_failure_exit_codes(tmp_path, monkeypatch, capsys):
    import palinreplay.cli.preprocess as cli

    class Config:
        llm = SimpleNamespace(model="m")

    monkeypatch.setattr(cli, "store_path", lambda path: tmp_path / "record")
    monkeypatch.setattr(cli, "record_name", lambda path: "task")
    monkeypatch.setattr(
        "palinreplay.preprocess.config.load_preprocess_config", lambda path: (Config(), path)
    )
    monkeypatch.setattr(
        "palinreplay.preprocess.pipeline.run_preprocess",
        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    assert main(["preprocess", str(tmp_path), "--output", "out"]) == 130
    assert "Preprocess cancelled" in capsys.readouterr().err

    monkeypatch.setattr(
        "palinreplay.preprocess.pipeline.run_preprocess",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("bad analysis")),
    )
    assert main(["preprocess", str(tmp_path), "--output", "out"]) == 1
    assert "bad analysis" in capsys.readouterr().err
