"""Real Btrfs incremental-discovery acceptance; no task image or model needed."""

import json
import sys
import time
from pathlib import Path
from uuid import uuid4

from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.btrfs.models import Snapshot
from palinreplay.storage.btrfs.pool import BtrfsPool


def main(output):
    started = time.monotonic()
    client = connect_docker(600)
    pool = BtrfsPool.create(client)
    try:
        with pool:
            def run(script):
                result = pool.container.exec_run(["python3", "-c", script])
                if result.exit_code:
                    raise RuntimeError(result.output.decode(errors="replace"))
                return result.output

            work = "workspaces/" + uuid4().hex
            run("import subprocess; from pathlib import Path; "
                f"p=Path('/pool/{work}'); subprocess.run(['btrfs','subvolume','create',str(p)],check=True); "
                "(p/'edit').write_text('old'); (p/'truncate').write_text('abcdef'); "
                "(p/'delete').write_text('gone'); (p/'mode').write_text('mode')")

            def snapshot():
                name = "checkpoints/" + uuid4().hex
                saved = pool.snapshot(work, name)
                return Snapshot(location=pool.location, name=name, uuid=saved["uuid"])

            before = snapshot()
            run("from pathlib import Path; "
                f"p=Path('/pool/{work}'); "
                "(p/'new').write_text('added'); (p/'edit').write_text('changed'); "
                "(p/'truncate').write_text('a'); (p/'delete').unlink(); (p/'mode').chmod(0o600)")
            after = snapshot()
            changes = pool.changes(before, after)
            by_path = {}
            for op in changes["operations"]:
                by_path.setdefault(op.get("path"), []).append(op["command"])
            assert "update_extent" in by_path["edit"]
            assert "truncate" in by_path["truncate"]
            assert "unlink" in by_path["delete"]
            assert "chmod" in by_path["mode"]
            assert any(op["command"] == "rename" and op.get("to") == "new"
                       for op in changes["operations"])
            pool.command("delete", name=work)
        report = {"status": "passed", "basic": changes,
                  "duration_sec": time.monotonic() - started, "cleanup_confirmed": True}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: v for k, v in report.items() if k != "basic"}))
    finally:
        try:
            pool.discard()
        finally:
            client.close()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
