"""Replay one generated manifest across the documented start/end matrix.

Run from this worktree with Docker available. The source Record is read-only;
each row writes to its own new directory and the manifest path is passed unchanged.
"""

import argparse
import hashlib
import json
from pathlib import Path

import replay_service_fixture as fixture

from palinreplay.preprocess.manifest import load_manifest, select_services
from palinreplay.replay.session import run_replay
from palinreplay.storage.io import write_json

ROWS = [
    ("C0", "C40", []),
    ("C19", "C26", ["A", "B"]),
    ("C19", "C40", ["A", "B"]),
    ("C25", "C30", ["B"]),
    ("C33", "C38", []),
    ("C35", "C38", ["C"]),
]


def main(source, manifest_path, output, run_id):
    source, manifest_path, output = source.resolve(), manifest_path.resolve(), output.resolve()
    source = source if (source / "catalog.sqlite").exists() else source / "record"
    output.mkdir(parents=True, exist_ok=False)
    manifest = load_manifest(manifest_path)
    before = fixture.store_hashes(source)
    summary = {
        "status": "failed",
        "source": str(source),
        "manifest": str(manifest_path),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "source_selection_sha256": manifest.source.selection_sha256,
        "rows": [],
    }
    write_json(output / "acceptance.json", summary)
    try:
        for start, end, expected in ROWS:
            row = {"from": start, "to": end, "expected": expected}
            summary["rows"].append(row)
            selected = [service.name for service in select_services(manifest, int(start[1:]))]
            assert selected == expected, (start, selected, expected)
            target = output / f"{start}_{end}"
            report = run_replay(
                source, start, end, run_id=run_id, output=target,
                services_config=manifest_path,
            )
            row.update(
                cli_equivalent_exit=0 if report["execution_status"] == "completed" else 2,
                execution_status=report["execution_status"],
                selected=report.get("services_manifest", {}).get("selected"),
                preparation_status=(report.get("preparation") or {}).get("status"),
                actual_node_count=report.get("actual_node_count"),
                completed_steps=report.get("completed_steps"),
                selected_steps=report.get("selected_steps"),
                cleanup_confirmed=report.get("cleanup_confirmed"),
                error=report.get("error"),
                exit_mismatch=report.get("exit_mismatch"),
                output=str(target),
            )
            assert row["execution_status"] == "completed", row
            assert row["selected"] == expected, row
            assert row["preparation_status"] == "ready", row
            assert row["cleanup_confirmed"] is True, row
            assert fixture.store_hashes(source) == before
            write_json(output / "acceptance.json", summary, replace=True)
        summary.update(status="passed", source_unchanged=True)
    except BaseException as error:
        summary.update(error_type=type(error).__name__, error=str(error))
        raise
    finally:
        summary["source_unchanged"] = fixture.store_hashes(source) == before
        write_json(output / "acceptance.json", summary, replace=True)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--run", required=True, dest="run_id")
    args = parser.parse_args()
    main(args.source, args.manifest, args.output, args.run_id)
