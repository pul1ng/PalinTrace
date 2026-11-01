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
            # Branch from one known-equal baseline. Include one-sided changes and
            # different writes on both sides; compare union against full inventory.
            baseline = snapshot()
            run("from pathlib import Path; " + f"p=Path('/pool/{work}'); "
                "(p/'source-only').write_text('source'); (p/'edit').write_text('source change')")
            source_after = snapshot()
            actual_work = 'workspaces/' + uuid4().hex
            pool.snapshot(baseline.name, actual_work, readonly=False)
            run("from pathlib import Path; " + f"p=Path('/pool/{actual_work}'); "
                "(p/'actual-only').write_text('actual'); (p/'edit').write_text('actual change')")
            actual_name = 'checkpoints/' + uuid4().hex
            actual_info = pool.snapshot(actual_work, actual_name)
            actual_after = Snapshot(location=pool.location, name=actual_name, uuid=actual_info['uuid'])
            compared = pool.compare_incremental(baseline, source_after, baseline, actual_after)
            full = json.loads(run(
                "import sys,json,importlib.util; sys.path.insert(0,'/'); "
                "s=importlib.util.spec_from_file_location('w','/palin-btrfs-worker.py'); "
                "w=importlib.util.module_from_spec(s); s.loader.exec_module(w); "
                "scope={'id':'rootfs-files-v1','exclude_paths':[], 'exclude_subtrees':[]}; "
                f"print(json.dumps(w.compare(w.inventory(w.target('{source_after.name}'),scope),"
                f"w.inventory(w.target('{actual_after.name}'),scope))))"
            ))
            assert compared['equal'] == full['equal'] is False
            assert compared['different_paths'] == full['different_paths']
            pool.command('delete', name=actual_work)
            root_baseline = snapshot()
            root_cases = []
            for attribute, mutation in (
                ("mode", "p.chmod(0o700)"),
                ("uid", "os.chown(p,123,-1)"),
                ("gid", "os.chown(p,-1,456)"),
            ):
                root_work = 'workspaces/' + uuid4().hex
                pool.snapshot(root_baseline.name, root_work, readonly=False)
                run("import os; from pathlib import Path; "
                    f"p=Path('/pool/{root_work}'); " + mutation)
                root_name = 'checkpoints/' + uuid4().hex
                saved = pool.snapshot(root_work, root_name)
                root_after = Snapshot(location=pool.location, name=root_name, uuid=saved['uuid'])
                root_result = pool.compare_incremental(
                    root_baseline, root_baseline, root_baseline, root_after)
                root_full = json.loads(run(
                    "import sys,json,importlib.util; sys.path.insert(0,'/'); "
                    "s=importlib.util.spec_from_file_location('w','/palin-btrfs-worker.py'); "
                    "w=importlib.util.module_from_spec(s); s.loader.exec_module(w); "
                    "scope={'id':'rootfs-files-v1','exclude_paths':[], 'exclude_subtrees':[]}; "
                    f"print(json.dumps(w.compare(w.inventory(w.target('{root_baseline.name}'),scope),"
                    f"w.inventory(w.target('{root_after.name}'),scope))))"
                ))
                assert root_result['equal'] == root_full['equal'] is False
                assert root_result['different_paths'] == root_full['different_paths'] == ['.']
                assert root_result['read_bytes'] == 0
                root_cases.append({'attribute': attribute, 'incremental': root_result,
                                   'full_comparison': root_full})
                pool.command('delete', name=root_work)
            # A directory rename with 1,800 long file names exceeds Linux's
            # single exec-argument limit when candidates are encoded inline.
            wide_old, wide_new = 'old-' + 'x' * 24, 'new-' + 'x' * 24
            run("from pathlib import Path; " + f"p=Path('/pool/{work}'); "
                f"d=p/{wide_old!r}; d.mkdir(); "
                "[(d/(f'{i:04}-'+'y'*24)).write_bytes(b'fixed') for i in range(1800)]")
            wide_before = snapshot()
            run("from pathlib import Path; " + f"p=Path('/pool/{work}'); "
                f"(p/{wide_old!r}).rename(p/{wide_new!r})")
            wide_after = snapshot()
            wide_match = pool.compare_incremental(wide_before, wide_after, wide_before, wide_after)
            assert wide_match['equal'] and wide_match['candidate_path_count'] >= 3602
            request_bytes = len(json.dumps(wide_match['candidate_paths']).encode())
            assert request_bytes > 200_000
            wide_full = json.loads(run(
                "import sys,json,importlib.util; sys.path.insert(0,'/'); "
                "s=importlib.util.spec_from_file_location('w','/palin-btrfs-worker.py'); "
                "w=importlib.util.module_from_spec(s); s.loader.exec_module(w); "
                "scope={'id':'rootfs-files-v1','exclude_paths':[], 'exclude_subtrees':[]}; "
                f"print(json.dumps(w.compare(w.inventory(w.target('{wide_after.name}'),scope),"
                f"w.inventory(w.target('{wide_after.name}'),scope))))"
            ))
            assert wide_match['equal'] == wide_full['equal']
            run("from pathlib import Path; " + f"p=Path('/pool/{work}'); "
                f"(p/{wide_new!r}/('0000-'+'y'*24)).write_bytes(b'different')")
            wide_changed = snapshot()
            wide_difference = pool.compare_incremental(
                wide_before, wide_after, wide_before, wide_changed)
            wide_different_full = json.loads(run(
                "import sys,json,importlib.util; sys.path.insert(0,'/'); "
                "s=importlib.util.spec_from_file_location('w','/palin-btrfs-worker.py'); "
                "w=importlib.util.module_from_spec(s); s.loader.exec_module(w); "
                "scope={'id':'rootfs-files-v1','exclude_paths':[], 'exclude_subtrees':[]}; "
                f"print(json.dumps(w.compare(w.inventory(w.target('{wide_after.name}'),scope),"
                f"w.inventory(w.target('{wide_changed.name}'),scope))))"
            ))
            assert wide_difference['equal'] == wide_different_full['equal'] is False
            assert wide_difference['different_paths'] == wide_different_full['different_paths']
            remaining = json.loads(run(
                "import json; from pathlib import Path; "
                "print(json.dumps([p.name for p in Path('/tmp').glob('palin-btrfs-request-*.json')]))"
            ))
            assert not remaining
            pool.command("delete", name=work)
        report = {"status": "passed", "basic": changes, "renames": renamed,
                  "hardlinks": link_cases, "comparison": compared, "full_comparison": full,
                  "root_attributes": root_cases,
                  "large_candidates": {'file_count': 1800, 'json_bytes': request_bytes,
                                       'match': wide_match, 'full_match': wide_full,
                                       'difference': wide_difference,
                                       'full_difference': wide_different_full,
                                       'request_files_remaining': remaining},
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
