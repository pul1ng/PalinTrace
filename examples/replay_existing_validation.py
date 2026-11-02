"""Validate two preselected existing Astropy intervals once in strict validation mode."""

import argparse
import json
import time
from pathlib import Path

from r1_restore_validation import absent, store_hashes

from palinreplay.record.restore import load_restore_materials
from palinreplay.replay.session import run_replay
from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.io import write_json
from palinreplay.storage.store import RecordStore


def main(source, output):
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    before = store_hashes(source)
    client = connect_docker(600)
    try:
        with RecordStore.open(source) as store:
            baseline = load_restore_materials(store, "C20")
            assert absent(client, baseline.runtime.source_container_id)
        reports = []
        # Selected from read-only inspection BEFORE execution. No retries or
        # alternate ranges are chosen after observing their outcomes.
        for name, start, end in (("read-only-C20-C23", "C20", "C23"),
                                 ("write-C2-C4", "C2", "C4")):
            report = run_replay(source, start, end, output=output / name, client=client,
                                validation_mode="strict")
            reports.append({"name": name, "from": start, "to": end, "report": report})
            print(json.dumps({"name": name, "status": report["status"],
                              "reason": report.get("stop_reason"),
                              "error": report.get("error")}), flush=True)
        unchanged = before == store_hashes(source)
        summary = {"source_unchanged": unchanged, "source_container_absent": True,
                   "duration_sec": time.monotonic() - started, "runs": reports}
        write_json(output / "summary.json", summary)
        assert unchanged
        assert all(r["report"]["cleanup_confirmed"] for r in reports)
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    main(args.source, args.output)
