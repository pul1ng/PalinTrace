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
    execution = {"match": "completed", "difference": "stopped", "failed": "failed"}[status]
    validation = "incomplete" if status == "failed" else status
    report = {"status": status, "execution_status": execution, "validation_status": validation}
    monkeypatch.setattr(session, "run_replay", lambda *a, **kw: seen.append((a, kw)) or report)
    assert main(["replay", str(tmp_path), "--from", "C20"]) == code
    assert seen[0][0] == (tmp_path / "record", "C20", None)
    assert seen[0][1]["output"] == default_output("replay", "task")
    assert seen[0][1]["continue_on_difference"] is False
    output = capsys.readouterr().out
    assert execution.upper() in output and f"Validation: {validation.upper()}" in output
    assert main(["replay", str(tmp_path), "--from", "C20", "--to", "C23",
                 "--run", "run-id", "--output", "custom", "--continue-on-difference"]) == code
    assert seen[1][1]["output"] == Path("custom") and seen[1][1]["run_id"] == "run-id"
    assert seen[1][1]["continue_on_difference"] is True


@pytest.mark.parametrize("execution,validation,code", [
    ("completed", "difference", 2), ("stopped", "difference", 2),
    ("failed", "difference", 1), ("failed", "match", 1),
])
def test_cli_reports_execution_and_validation_independently(
    tmp_path, monkeypatch, capsys, execution, validation, code,
):
    import palinreplay.cli.replay as cli
    import palinreplay.replay.session as session

    report = {"status": "failed" if execution == "failed" else validation,
              "execution_status": execution, "validation_status": validation}
    if validation == "difference":
        report["first_difference"] = {"difference_reason": "file-state", "source_index": 3}
    if execution == "failed":
        report["error"] = "cleanup fault"
    monkeypatch.setattr(cli, "store_path", lambda path: path)
    monkeypatch.setattr(session, "run_replay", lambda *a, **kw: report)
    assert main(["replay", str(tmp_path), "--from", "C2", "--to", "C4",
                 "--output", "custom", "--continue-on-difference"]) == code
    captured = capsys.readouterr()
    assert f"Replay {execution.upper()}" in captured.out
    assert f"Validation: {validation.upper()}" in captured.out
    assert ("Stopped on difference" in captured.out) == (execution == "stopped")
    if validation == "difference":
        assert "MATCH" not in captured.out and "source C3" in captured.out
    if execution == "failed":
        assert "cleanup fault" in captured.err


def test_continue_flag_is_documented_in_help(capsys):
    with pytest.raises(SystemExit) as exit:
        main(["replay", "--help"])
    assert exit.value.code == 0
    assert "--continue-on-difference" in capsys.readouterr().out
