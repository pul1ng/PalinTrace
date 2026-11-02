"""Validate the existing Astropy C2→C4 range through the CLI in both difference modes."""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from r1_restore_validation import absent, store_hashes

from palinreplay.core.models import Node, Run, State
from palinreplay.record.calls import read_call
from palinreplay.record.restore import load_restore_materials
from palinreplay.replay.plan import pin_replay
from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.btrfs.models import Snapshot
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.io import write_json
from palinreplay.storage.store import RecordStore


def check_record(output, report, continuation):
    evidence, snapshots = [], []
    with RecordStore.open(output / "record") as store:
        run = store.get(Run, report["run_id"])
        assert store.read_json(run.config)["replay"]["continue_on_difference"] is continuation
        nodes = sorted(store.list(Node, run_id=run.run_id), key=lambda node: node.index)
        assert len(nodes) == len(report["mappings"])
        for mapping, node in zip(report["mappings"], nodes, strict=True):
            assert mapping["node_id"] == node.node_id and mapping["index"] == node.index
            snapshot = Snapshot.model_validate(store.read_json(store.get(State, node.state_id).manifest))
            snapshots.append(snapshot)
            if node.index == 0:
                continue
            entry = report["steps"][node.index - 1]
            assert entry["node_id"] == node.node_id
            assert [tool["execution_id"] for tool in entry["tools"]] == list(node.tool_execution_ids)
            model = read_call(store, node.model_execution_id)
            assert model.operation.name == "replay.history" and model.phase == "accepted"
            model_result = store.read_json(model.events[1].details)["backend"]
            assert model_result["actual_model_calls"] == model_result["actual_model_cost"] == 0
            context, position = store.read_json(node.context), store.read_json(node.input_position)
            assert context["replay"]["actual_model_calls"] == context["replay"]["actual_model_cost"] == 0
            for compared in entry["tools"]:
                actual = read_call(store, compared["execution_id"])
                assert actual.phase == "accepted" and compared["definitive"]
                assert actual.execution.execution_id != compared["source_execution_id"]
                raw = store.read_bytes(actual.events[1].payload)
                result = store.read_json(actual.events[1].details)["backend"]["decoded"]
                assert hashlib.sha256(raw).hexdigest() == compared["actual_output_sha256"]
                assert result["returncode"] == compared["actual_exit_code"]
                accepted = store.read_json(actual.events[-1].payload)["messages"]
                assert all(message in context["messages"] for message in accepted)
                assert compared["execution_id"] in [item["execution_id"] for item in position["adoptions"]]
            assert "files" in entry
            evidence.append({"source_index": mapping["source_index"], "actual_index": node.index,
                             "actual_tools": len(entry["tools"]),
                             "tool_differences": sum(not tool["equal"] for tool in entry["tools"]),
                             "file_differences": len(entry["files"]["different_paths"]),
                             "starting_divergent": entry["starting_divergent"],
                             "actual_context_and_results_verified": True,
                             "local_history_without_model_requests": True})
    return evidence, snapshots


def main(source, output):
    source, output = source.absolute(), output.absolute()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    started = time.monotonic()
    before = store_hashes(source)
    write_json(output / "source-before.json", before)
    summary = {"source": str(source), "from": "C2", "to": "C4", "runs": []}
    client = connect_docker(600)
    try:
        summary["daemon_id"] = client.info()["ID"]
        with RecordStore.open(source) as store:
            baseline = load_restore_materials(store, "C2")
            plan = pin_replay(store, "C2", "C4")
            source_snapshots = [Snapshot.model_validate(store.read_json(state.manifest))
                                for state in plan.states]
        assert absent(client, baseline.runtime.source_container_id)
        summary["source_container_absent"] = True
        snapshots, workspaces = list(source_snapshots), []
        helper = None
        for name, continuation, indices in (("strict", False, [2, 3]), ("continue", True, [2, 3, 4])):
            target = output / name
            command = [str(Path(sys.executable).with_name("palin")), "replay", str(source),
                       "--from", "C2", "--to", "C4", "--output", str(target)]
            if continuation:
                command.append("--continue-on-difference")
            result = subprocess.run(command, capture_output=True, text=True)
            (output / f"{name}.stdout.txt").write_text(result.stdout)
            (output / f"{name}.stderr.txt").write_text(result.stderr)
            report = json.loads((target / "report.json").read_text())
            row = {"name": name, "command": command, "exit_code": result.returncode,
                   "execution_status": report["execution_status"],
                   "validation_status": report["validation_status"],
                   "report": str(target / "report.json")}
            summary["runs"].append(row)
            print(json.dumps(row), flush=True)
            assert result.returncode == 2 and "MATCH" not in result.stdout
            assert report["execution_status"] == ("completed" if continuation else "stopped")
            assert report["validation_status"] == report["status"] == "difference"
            assert [mapping["source_index"] for mapping in report["mappings"]] == indices
            assert [entry["source_index"] for entry in report["steps"]] == indices[1:]
            assert report["first_difference"] == report["steps"][0]["files"]
            assert report["first_difference"]["source_index"] == 3
            assert report["first_difference"]["different_paths"]
            assert not report["steps"][0]["starting_divergent"]
            if continuation:
                assert report["steps"][1]["starting_divergent"]
                assert "stop_reason" not in report and "Stopped" not in result.stdout
            else:
                assert report["stop_reason"] == "file-state"
            stats = report["statistics"]
            assert stats["actual_model_calls"] == stats["actual_model_cost"] == 0
            assert stats["historical_answer_supplies"] == len(indices) - 1
            row["steps"], actual_snapshots = check_record(target, report, continuation)
            snapshots.extend(actual_snapshots)
            assert stats["actual_tool_executions"] == sum(step["actual_tools"] for step in row["steps"])
            restored = json.loads((target / "restore.json").read_text())
            assert report["cleanup_confirmed"] and restored["cleanup_confirmed"]
            helper = restored["pool_container_name"]
            assert absent(client, restored["container_name"]) and absent(client, helper)
            assert not client.volumes.list(filters={"name": "^" + restored["volume_name"] + "$"})
            workspaces.append(restored["workspace"])
            row["temporary_container_helper_and_volume_absent"] = True

        # Metadata-only audit: retained immutable snapshots and deleted workspaces.
        with BtrfsPool(client, baseline.snapshot.location) as pool:
            for snapshot in snapshots:
                pool.verify(snapshot)
            audit = pool.container.exec_run([
                "python3", "-I", "-c",
                "import json,sys; from pathlib import Path; "
                "print(json.dumps({name:not (Path('/pool')/name).exists() for name in sys.argv[1:]}))",
                *workspaces,
            ])
            assert audit.exit_code == 0
            summary["workspace_absent"] = json.loads(audit.output)
            assert all(summary["workspace_absent"].values())
        assert absent(client, helper)
        summary["immutable_snapshot_identities_verified"] = len(snapshots)
        summary["audit_helper_cleaned"] = True
    except BaseException as error:
        summary["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        after = store_hashes(source)
        summary["source_unchanged"] = before == after
        summary["source_files_hashed"] = len(before)
        summary["duration_sec"] = time.monotonic() - started
        write_json(output / "source-after.json", after)
        write_json(output / "summary.json", summary)
        client.close()
    assert summary["source_unchanged"]
    print(json.dumps({"source_unchanged": True, "duration_sec": summary["duration_sec"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    main(args.source, args.output)
