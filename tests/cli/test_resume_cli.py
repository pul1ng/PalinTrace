from pathlib import Path

import pytest

from palinreplay.cli import main
from palinreplay.cli.app import build_parser


def test_resume_help(capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["--help"])
    assert stopped.value.code == 0
    assert "resume" in capsys.readouterr().out
    with pytest.raises(SystemExit) as stopped:
        main(["resume", "--help"])
    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    for option in ("SOURCE", "--from", "--run", "--max-steps", "--llm-config",
                   "--instruction", "--services-config", "--output"):
        assert option in help_text
    assert "default: 20" in help_text
    assert "including the first request" in help_text
    assert "submit early" in help_text
    assert "--to" not in help_text and "--progress" not in help_text


@pytest.mark.parametrize("options", [
    [], ["source"], ["--from", "C20"],
    ["source", "--from", "C20", "--max-steps", "0"],
    ["source", "--from", "C20", "--max-steps", "-1"],
    ["source", "--from", "C20", "--max-steps", "1.5"],
    ["source", "--from", "C20", "--instruction", ""],
    ["source", "--from", "C20", "--instruction", " \n\t"],
])
def test_invalid_arguments_stop_before_execution(monkeypatch, capsys, options):
    import palinreplay.cli.resume as cli

    def forbidden(*args, **kwargs):
        pytest.fail("invalid input must not reach Docker or model execution")

    monkeypatch.setattr(cli, "execute", forbidden)
    with pytest.raises(SystemExit) as stopped:
        main(["resume", *options])
    assert stopped.value.code == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("budget", [1, 2, 100])
def test_all_parameters_are_parsed_and_dispatched_unchanged(monkeypatch, budget):
    import palinreplay.cli.resume as cli

    seen = []
    monkeypatch.setattr(cli, "execute", lambda args: seen.append(args) or 7)
    instruction = "  下一步先检查 B 的日志\n"
    assert main([
        "resume", "source/record", "--from", "node-id", "--run", "run-id",
        "--max-steps", str(budget), "--llm-config", "model.toml",
        "--instruction", instruction, "--services-config", "services.json", "--output", "branch",
    ]) == 7
    args = seen[0]
    assert (args.source, args.start, args.run_id) == (Path("source/record"), "node-id", "run-id")
    assert args.max_steps == budget
    assert args.llm_config == Path("model.toml")
    assert args.instruction == instruction
    assert args.services_config == Path("services.json")
    assert args.output == Path("branch")


def test_resume_defaults():
    args = build_parser().parse_args(["resume", "source", "--from", "C20"])
    assert args.max_steps == 20
    assert args.run_id is args.llm_config is args.instruction is args.services_config is None
    assert args.output is None
