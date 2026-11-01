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
    monkeypatch.setattr(session, "run_replay", lambda *a, **kw: seen.append((a, kw)) or {"status": status})
    assert main(["replay", str(tmp_path), "--from", "C20"]) == code
    assert seen[0][0] == (tmp_path / "record", "C20", None)
    assert seen[0][1]["output"] == default_output("replay", "task")
    assert status.upper() in capsys.readouterr().out
    assert main(["replay", str(tmp_path), "--from", "C20", "--to", "C23",
                 "--run", "run-id", "--output", "custom"]) == code
    assert seen[1][1]["output"] == Path("custom") and seen[1][1]["run_id"] == "run-id"
