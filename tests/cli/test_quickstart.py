import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/quickstart_record.sh"


@pytest.mark.parametrize("mode", ["default", "custom", "record-fails"])
def test_quickstart_groups_outputs_and_stops_after_failure(tmp_path, mode):
    project = tmp_path / "project with spaces"
    script = project / "scripts/quickstart_record.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, script)
    executable = project / ".venv/bin/palin"
    executable.parent.mkdir(parents=True)
    executable.write_text(f"#!{sys.executable}\n" + '''
import json
import os
import sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["PALIN_QUICKSTART_CALL_LOG"]).open("a") as handle:
    handle.write(json.dumps(args) + "\\n")
if os.environ.get("PALIN_QUICKSTART_FAIL") and args[:2] == ["record", "run"]:
    sys.exit(9)
for flag in ("--output", "--report"):
    if flag in args:
        path = Path(args[args.index(flag) + 1])
        if flag == "--report" or args[:2] == ["record", "visualize"]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        else:
            path.mkdir(parents=True)
''')
    executable.chmod(0o755)
    launch = tmp_path / "launch"
    launch.mkdir()
    root = launch / ("custom-root" if mode == "custom" else "outputs")
    root.mkdir()
    (root / "keep").write_text("existing result")
    log = tmp_path / "calls.jsonl"
    env = dict(os.environ, PALIN_QUICKSTART_CALL_LOG=str(log))
    if mode == "record-fails":
        env["PALIN_QUICKSTART_FAIL"] = "1"
    args = ["bash", str(script)] + (["--output", "custom-root"] if mode == "custom" else [])
    result = subprocess.run(args, cwd=launch, env=env, capture_output=True, text=True)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert result.returncode == (9 if mode == "record-fails" else 0), result.stderr
    assert (root / "keep").read_text() == "existing result"
    if mode == "record-fails":
        assert calls[-1][:2] == ["record", "run"]
        assert not (root / "visual").exists()
        return
    assert calls[-1][:2] == ["record", "visualize"] and len(calls) == 10
    products = [path for category in root.iterdir() if category.is_dir() for path in category.iterdir()]
    assert {path.parent.name for path in products} == {
        "download", "convert", "preload", "record", "verify-restore", "export", "visual",
    }
    assert all(re.match(r"\d{6}_\d{6}_", path.name) for path in products)
    assert len({path.name[:13] for path in products}) == 1
    assert not (project / "outputs").exists()
