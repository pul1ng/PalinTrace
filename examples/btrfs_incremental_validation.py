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
            # Rename chains, directory descendants and replacement of an old name.
            run("from pathlib import Path; "
                f"p=Path('/pool/{work}'); (p/'dir').mkdir(); "
                "(p/'dir'/'child').write_text('before'); (p/'victim').write_text('victim')")
            rename_before = snapshot()
            run("from pathlib import Path; "
                f"p=Path('/pool/{work}'); (p/'dir').rename(p/'tmp'); "
                "(p/'tmp').rename(p/'moved'); (p/'moved'/'child').write_text('after'); "
                "(p/'edit').rename(p/'victim')")
            rename_after = snapshot()
            renamed = pool.changes(rename_before, rename_after)
            assert {'dir', 'dir/child', 'moved', 'moved/child', 'edit', 'victim'} <= set(
                renamed['candidates'])
            # Explicit full comparison is an acceptance oracle, not discovery.
            oracle = run(
                "import sys,json; sys.path.insert(0,'/'); "
                "import importlib.util; "
                "s=importlib.util.spec_from_file_location('w','/palin-btrfs-worker.py'); "
                "w=importlib.util.module_from_spec(s); s.loader.exec_module(w); "
                "scope={'exclude_paths':[],'exclude_subtrees':[]}; "
                f"a=w.inventory(w.target('{rename_before.name}'),scope); "
                f"b=w.inventory(w.target('{rename_after.name}'),scope); "
                "print(json.dumps([p for p in a['entries'].keys()|b['entries'].keys() "
                "if a['entries'].get(p)!=b['entries'].get(p)]))"
            )
            assert set(json.loads(oracle)) <= set(renamed['candidates'])
            link_cases = []
            run("import os; from pathlib import Path; "
                f"p=Path('/pool/{work}'); (p/'linked').write_text('same'); "
                "os.link(p/'linked',p/'alias\\nname')")
            link_before = snapshot()
            for mutation, expected in [
                ("(p/'linked').write_text('written through alias')", {'linked', 'alias\nname'}),
                ("os.link(p/'linked',p/'third')", {'linked', 'alias\nname', 'third'}),
                ("(p/'third').unlink()", {'linked', 'alias\nname', 'third'}),
                ("data=(p/'linked').read_bytes(); (p/'linked').unlink(); "
                 "(p/'linked').write_bytes(data)", {'linked', 'alias\nname'}),
            ]:
                run("import os; from pathlib import Path; " + f"p=Path('/pool/{work}'); " + mutation)
                link_after = snapshot()
                delta = pool.changes(link_before, link_after)
                assert expected <= set(delta['candidates']), delta
                link_cases.append(delta)
                link_before = link_after
            pool.command("delete", name=work)
        report = {"status": "passed", "basic": changes, "renames": renamed,
                  "hardlinks": link_cases,
                  "duration_sec": time.monotonic() - started, "cleanup_confirmed": True}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: v for k, v in report.items()
                          if k in ("status", "duration_sec", "cleanup_confirmed")}))
    finally:
        try:
            pool.discard()
        finally:
            client.close()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
