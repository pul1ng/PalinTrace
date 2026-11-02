from pathlib import Path

import pytest

from palinreplay.cli import main
from palinreplay.cli.outputs import default_output


@pytest.mark.parametrize("status,code", [("match", 0), ("difference", 2), ("failed", 1)])
def test_minimal_cli_paths_and_status(tmp_path, monkeypatch, capsys, status, code):
    import palinreplay.cli.replay as cli
    import palinreplay.replay.session as session

    seen = []
    monkeypatch.setattr(cli, "store_path", lambda p: p / "record")
    monkeypatch.setattr(cli, "record_name", lambda p: "task")
    monkeypatch.setattr("palinreplay.cli.outputs.timestamp", lambda: "261101_220000")

    def replay(*args, **kwargs):
        seen.append((args, kwargs))
        mode = kwargs["validation_mode"]
        execution = "failed" if status == "failed" else "completed"
        if status == "difference" and mode == "strict":
            execution = "stopped"
        return {"status": status, "execution_status": execution, "validation_mode": mode,
                "validation_status": "incomplete" if status == "failed" else status}

    monkeypatch.setattr(session, "run_replay", replay)
    assert main(["replay", str(tmp_path), "--from", "C20"]) == code
    assert seen[0][0] == (tmp_path / "record", "C20", None)
    assert seen[0][1]["output"] == default_output("replay", "task")
    assert seen[0][1]["validation_mode"] == "lenient"
    assert callable(seen[0][1]["progress"])
    output = capsys.readouterr().out
    assert "Validation mode: lenient" in output
    assert f"Validation: {'INCOMPLETE' if status == 'failed' else status.upper()}" in output
    for index, mode in enumerate(("lenient", "strict"), start=1):
        assert main(["replay", str(tmp_path), "--from", "C20", "--to", "C23",
                     "--run", "run-id", "--output", "custom", "--validation", mode]) == code
        assert seen[index][1]["output"] == Path("custom")
        assert seen[index][1]["run_id"] == "run-id"
        assert seen[index][1]["validation_mode"] == mode
        assert f"Validation mode: {mode}" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["strict", "lenient"])
@pytest.mark.parametrize("execution,validation,code", [
    ("completed", "match", 0), ("completed", "difference", 2), ("stopped", "difference", 2),
    ("failed", "incomplete", 1), ("failed", "difference", 1), ("failed", "match", 1),
])
def test_cli_reports_mode_execution_and_validation_independently(
    tmp_path, monkeypatch, capsys, mode, execution, validation, code,
):
    import palinreplay.cli.replay as cli
    import palinreplay.replay.session as session

    report = {"status": "failed" if execution == "failed" else validation,
              "validation_mode": mode,
              "execution_status": execution, "validation_status": validation}
    if validation == "difference":
        report["first_difference"] = {"difference_reason": "file-state", "source_index": 3}
    if execution == "failed":
        report["error"] = "cleanup fault"
    monkeypatch.setattr(cli, "store_path", lambda path: path)
    monkeypatch.setattr(session, "run_replay", lambda *a, **kw: report)
    assert main(["replay", str(tmp_path), "--from", "C2", "--to", "C4",
                 "--output", "custom", "--validation", mode]) == code
    captured = capsys.readouterr()
    assert f"Replay {execution.upper()}" in captured.out
    assert f"Validation mode: {mode}" in captured.out
    assert f"Validation: {validation.upper()}" in captured.out
    assert ("Stopped on difference" in captured.out) == (execution == "stopped")
    if validation == "difference":
        assert "MATCH" not in captured.out and "source C3" in captured.out
    if execution == "failed":
        assert "cleanup fault" in captured.err


def test_validation_modes_are_documented_in_help(capsys):
    with pytest.raises(SystemExit) as exit:
        main(["replay", "--help"])
    assert exit.value.code == 0
    output = capsys.readouterr().out
    assert "--validation {strict,lenient}" in output and "default: lenient" in output
    assert "--continue-on-difference" not in output


@pytest.mark.parametrize("options", [
    ["--validation", "unknown"], ["--continue-on-difference"],
])
def test_invalid_or_removed_mode_option_is_rejected_before_execution(tmp_path, monkeypatch, options):
    import palinreplay.replay.session as session

    def forbidden(*args, **kwargs):
        pytest.fail("invalid replay options must not execute a replay")

    monkeypatch.setattr(session, "run_replay", forbidden)
    with pytest.raises(SystemExit) as exit:
        main(["replay", str(tmp_path), "--from", "C2", *options])
    assert exit.value.code == 2


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
        progress(ProgressEvent("finished", status="completed", label="COMPLETED / DIFFERENCE"))
        return {"validation_mode": "lenient", "execution_status": "completed",
                "validation_status": "difference"}

    monkeypatch.setattr(cli, "store_path", lambda path: path)
    monkeypatch.setattr(session, "run_replay", replay)
    options = [] if mode is None else ["--progress", mode]
    assert main(["replay", str(tmp_path), "--from", "C20", "--output", "custom", *options]) == 2
    output = capsys.readouterr()
    assert "Replay COMPLETED -> custom" in output.out and "Validation: DIFFERENCE" in output.out
    assert "[replay" not in output.out
    assert ("1/1" in output.err) is (mode != "off")
    assert "\x1b" not in output.err
