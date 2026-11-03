from pathlib import Path

import pytest

from palinreplay.cli import main
from palinreplay.cli.outputs import default_output


@pytest.mark.parametrize("status,code", [
    ("completed", 0), ("stopped", 2), ("failed", 1), ("cancelled", 130),
])
def test_minimal_cli_paths_and_status(tmp_path, monkeypatch, capsys, status, code):
    import palinreplay.cli.replay as cli
    import palinreplay.replay.session as session

    seen = []
    monkeypatch.setattr(cli, "store_path", lambda p: p / "record")
    monkeypatch.setattr(cli, "record_name", lambda p: "task")
    monkeypatch.setattr("palinreplay.cli.outputs.timestamp", lambda: "261101_220000")

    def replay(*args, **kwargs):
        seen.append((args, kwargs))
        return {"execution_status": status, "source_start": {"index": 20}, "actual_node_count": 0}

    monkeypatch.setattr(session, "run_replay", replay)
    assert main(["replay", str(tmp_path), "--from", "C20"]) == code
    assert seen[0][0] == (tmp_path / "record", "C20", None)
    assert seen[0][1]["output"] == default_output("replay", "task")
    assert seen[0][1]["output"] == Path("outputs/replay/261101_220000_task").absolute()
    assert "validation_mode" not in seen[0][1]
    assert seen[0][1]["services_config"] is None
    assert callable(seen[0][1]["progress"])
    output = capsys.readouterr().out
    assert f"Replay {status.upper()}" in output and "完整一致性校验未执行" in output
    assert "源 C20 (引用)" in output and "Validation:" not in output
    assert main(["replay", str(tmp_path), "--from", "C20", "--to", "C23",
                 "--run", "run-id", "--output", "custom",
                 "--services-config", "services.json"]) == code
    assert seen[1][1]["services_config"] == Path("services.json")
    assert seen[1][1]["output"] == Path("custom") and seen[1][1]["run_id"] == "run-id"


@pytest.mark.parametrize("failed", [False, True])
def test_cli_retains_exit_mismatch_with_framework_failure(tmp_path, monkeypatch, capsys, failed):
    import palinreplay.cli.replay as cli
    import palinreplay.replay.session as session

    report = {"execution_status": "failed" if failed else "stopped",
              "exit_mismatch": {"source_index": 21, "tool_ordinal": 2,
                                "source_exit_code": 0, "actual_exit_code": 1}}
    if failed:
        report["error"] = "cleanup fault"
    monkeypatch.setattr(cli, "store_path", lambda path: path)
    monkeypatch.setattr(session, "run_replay", lambda *a, **kw: report)
    assert main(["replay", str(tmp_path), "--from", "C20", "--to", "C21",
                 "--output", "custom"]) == (1 if failed else 2)
    captured = capsys.readouterr()
    assert "source C21, tool 2: 0 -> 1" in captured.out
    assert "完整一致性校验未执行" in captured.out and "MATCH" not in captured.out
    if failed:
        assert "cleanup fault" in captured.err


@pytest.mark.parametrize("options", [
    ["--validation", "strict"], ["--validation", "lenient"], ["--validation", "unknown"],
    ["--continue-on-difference"],
])
def test_invalid_or_removed_mode_option_is_rejected_before_execution(tmp_path, monkeypatch, options):
    import palinreplay.replay.session as session

    def forbidden(*args, **kwargs):
        pytest.fail("invalid replay options must not execute a replay")

    monkeypatch.setattr(session, "run_replay", forbidden)
    with pytest.raises(SystemExit) as exit:
        main(["replay", str(tmp_path), "--from", "C2", *options])
    assert exit.value.code == 2


def test_help_exposes_execution_options_without_validation_mode(capsys):
    with pytest.raises(SystemExit) as exit:
        main(["replay", "--help"])
    assert exit.value.code == 0
    output = capsys.readouterr().out
    assert "--validation" not in output and "--continue-on-difference" not in output
    for option in ("--from", "--to", "--run", "--output", "--services-config", "--progress"):
        assert option in output


def test_replay_cancellation_keeps_exit_code_130(tmp_path, monkeypatch, capsys):
    import palinreplay.cli.replay as cli
    import palinreplay.replay.session as session

    def cancel(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "store_path", lambda path: path)
    monkeypatch.setattr(session, "run_replay", cancel)
    assert main(["replay", str(tmp_path), "--from", "C2", "--output", "custom"]) == 130
    assert "Replay cancelled" in capsys.readouterr().err


@pytest.mark.parametrize("mode", [None, "plain", "off"])
def test_cli_progress_stream_does_not_change_report_output(tmp_path, monkeypatch, capsys, mode):
    import palinreplay.cli.replay as cli
    import palinreplay.replay.session as session
    from palinreplay.core.progress import ProgressEvent

    def replay(*args, **kwargs):
        progress = kwargs["progress"]
        progress(ProgressEvent("preparing", total_steps=1))
        progress(ProgressEvent("committed", source_step=21, step=1))
        progress(ProgressEvent("finished", status="completed", label="COMPLETED"))
        return {"execution_status": "completed"}

    monkeypatch.setattr(cli, "store_path", lambda path: path)
    monkeypatch.setattr(session, "run_replay", replay)
    options = [] if mode is None else ["--progress", mode]
    assert main(["replay", str(tmp_path), "--from", "C20", "--output", "custom", *options]) == 0
    output = capsys.readouterr()
    assert "Replay COMPLETED -> custom" in output.out and "完整一致性校验未执行" in output.out
    assert "[replay" not in output.out and ("1/1" in output.err) is (mode != "off")
    assert "\x1b" not in output.err
