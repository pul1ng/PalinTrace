"""Run prepared/unprepared C20-C40 acceptance against an existing overlap recording.

This checks execution, retained service identities and resource ownership. It does
not perform full trajectory consistency verification or recreate the source.
"""

import argparse
import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import replay_service_fixture as fixture

from palinreplay.cli import main as cli_main
from palinreplay.core.models import Node, Operation, Run, StateCapture, Step
from palinreplay.record.calls import read_call
from palinreplay.record.restore import load_restore_materials
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.replay.plan import pin_replay
from palinreplay.replay.service_config import load_service_input, resolve_services
from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.io import outside_store, write_json
from palinreplay.storage.store import RecordStore


def snapshot_evidence(client, source, actual):
    """Read identities at retained real boundaries, with no prepared-state placeholder."""
    with RecordStore.open(source) as origin, RecordStore.open(actual) as current:
        selected = [load_restore_materials(origin, f"C{i}") for i in (20, 35)]
        selected += [load_restore_materials(current, f"C{i}") for i in (21, 35, 38, 40)]
    script = """import json, sys
from pathlib import Path
rows = []
for root in map(Path, sys.argv[1:]):
    identities = {}
    for name in ('A', 'B', 'C'):
        path = root / 'opt/palin-replay-service-fixture' / name / 'service.json'
        identities[name] = json.loads(path.read_text()) if path.exists() else None
    product = root / 'testbed/.palin-service-demo/report.json'
    rows.append({'identities': identities,
                 'product': json.loads(product.read_text()) if product.exists() else None})
print(json.dumps(rows))
"""
    with BtrfsPool(client, selected[0].snapshot.location) as pool:
        for material in selected:
            pool.verify(material.snapshot)
        result = pool.container.exec_run([
            "python3", "-I", "-c", script, *["/pool/" + item.snapshot.name for item in selected],
        ])
        assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    evidence = dict(zip(("source_C20", "source_C35", "actual_C21", "actual_C35", "actual_C38", "actual_C40"),
                        rows, strict=True))
    for name in ("A", "B"):
        identity = evidence["actual_C21"]["identities"][name]
        assert identity and identity != evidence["source_C20"]["identities"][name]
    assert evidence["actual_C21"]["identities"]["C"] is None
    assert evidence["actual_C35"]["identities"]["C"]
    assert evidence["actual_C35"]["identities"]["C"] != evidence["source_C35"]["identities"]["C"]
    assert evidence["actual_C38"]["identities"]["C"] is None
    assert evidence["actual_C40"]["product"] == {"final_total": 56, "results": 8}
    evidence["preparation_state_captured"] = False
    return evidence


def check_resources(client, source, replay):
    restored = json.loads((replay / "restore.json").read_text())
    checks = {"container_removed": fixture.absent(client, restored["container_name"]),
              "root_volume_removed": fixture.volume_absent(client, restored["volume_name"]),
              "helper_removed": fixture.absent(client, restored["pool_container_name"])}
    assert all(checks.values()) and restored["cleanup_confirmed"]
    with RecordStore.open(source) as store:
        baseline = load_restore_materials(store, "C20")
    assert fixture.absent(client, baseline.runtime.source_container_id)
    retained = []
    with BtrfsPool(client, baseline.snapshot.location) as pool:
        result = pool.container.exec_run([
            "python3", "-I", "-c", "from pathlib import Path; assert not Path('/pool', "
            + repr(restored["workspace"]) + ").exists()",
        ])
        assert result.exit_code == 0, result.output
        for path in (source, replay / "record"):
            with RecordStore.open(path) as store:
                for node in sorted(store.list(Node), key=lambda item: item.index):
                    material = load_restore_materials(store, node.node_id)
                    pool.verify(material.snapshot)
                    retained.append({"store": str(path), "index": node.index,
                                     "snapshot": material.snapshot.model_dump(mode="json")})
    assert fixture.absent(client, pool.name)
    client.volumes.get(baseline.snapshot.location.volume)
    return {**checks, "workspace_removed": True, "retained_readonly_snapshots": retained,
            "pool_close_confirmed": pool.closed, "persistent_pool_retained": True}


def run_case(source, target, services, scenario, selection, client):
    executed = []
    execute = RestoredExecutor.execute
    def observe(executor, request):
        if services is not None:
            assert json.loads((target / "preparation.json").read_text())["status"] == "ready"
        executed.append(request)
        return execute(executor, request)
    command = ["replay", str(source), "--from", "C20", "--to", "C40",
               "--run", selection["run"]["run_id"], "--output", str(target), "--progress", "plain"]
    if services is not None:
        command += ["--services-config", str(services)]
    stdout, stderr = StringIO(), StringIO()
    with fixture.no_model_calls() as models, patch.object(RestoredExecutor, "execute", observe), \
            redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli_main(command)
        assert models.call_count == 0
    (target / "cli.stdout.txt").write_text(stdout.getvalue())
    (target / "cli.stderr.txt").write_text(stderr.getvalue())
    report = json.loads((target / "report.json").read_text())
    prepared = services is not None
    assert code == (0 if prepared else 2), report
    assert report["schema_version"] == "palinreplay.replay-report.v2"
    assert report["execution_status"] == ("completed" if prepared else "stopped")
    assert "完整一致性校验未执行" in stdout.getvalue()
    assert report["source"] == selection and report["source_start"]["index"] == 20
    assert report["r1"]["status"] == "passed" and report["r1"]["files"]["equal"]
    assert report["cleanup_confirmed"] and not report["preparation_state_captured"]
    assert not (target / "preparation-files.json").exists()
    if prepared:
        journal = report["preparation"]
        assert journal["status"] == "ready"
        assert [s["name"] for s in journal["services"]] == ["A", "B"]
        assert all(s["status"] == "ready" and s["startup"]["exit_code"] == 0
                   and s["readiness"][-1]["exit_code"] == s["final_readiness"]["exit_code"] == 0
                   for s in journal["services"])
        assert [m["index"] for m in report["mappings"]] == list(range(21, 41))
    else:
        assert report["exit_mismatch"]["source_index"] == 21
        assert report["exit_mismatch"]["source_exit_code"] == 0
        assert report["exit_mismatch"]["actual_exit_code"] == 1
        assert len(executed) == len(report["steps"]) == 1
        assert all(m["index"] == 21 for m in report["mappings"])
    stats = report["statistics"]
    expected_calls = 20 if prepared else 1
    assert stats["actual_tool_executions"] == stats["historical_answer_supplies"] == expected_calls
    assert stats["historical_steps"] == expected_calls
    assert stats["actual_model_calls"] == stats["actual_model_cost"] == 0
    facts = []
    with RecordStore.open(source) as origin, RecordStore.open(target / "record") as actual:
        run = actual.get(Run, report["run_id"])
        config = actual.read_json(run.config)["replay"]
        assert run.source_node_id is None and run.run_id != selection["run"]["run_id"]
        assert config["source"] == selection
        start = origin.get(Node, selection["nodes"][0]["node_id"])
        assert config["source_start"]["context"] == origin.read_json(start.context)
        assert len(actual.list(Node)) == report["actual_node_count"] == report["completed_steps"]
        assert len(actual.list(StateCapture)) == report["actual_node_count"]
        assert len(actual.list(Step)) == expected_calls and len(actual.list(Operation)) == expected_calls * 2
        for i, entry in enumerate(report["steps"]):
            original = fixture.tool_fact(origin, load_restore_materials(origin, f"C{entry['source_index']}").node)
            evidence = entry["tools"][0]
            trace = read_call(actual, evidence["execution_id"])
            assert actual.read_json(trace.execution.request) == original["request"] == executed[i]
            raw = actual.read_bytes(trace.events[1].payload).decode("utf-8")
            decoded = actual.read_json(trace.events[1].details)["backend"]["decoded"]
            assert decoded["returncode"] == (0 if prepared else 1)
            row = {"source_index": entry["source_index"], "request": executed[i],
                   "execution_id": evidence["execution_id"], "result_ref": evidence["result_ref"],
                   "source_exit_code": original["exit_code"], "actual_exit_code": decoded["returncode"],
                   "raw_output": raw}
            if prepared:
                states = json.loads(next(line.removeprefix("PALIN_SERVICE_STATES ")
                                         for line in raw.splitlines() if line.startswith("PALIN_SERVICE_STATES ")))
                expected = next(s["expected_alive"] for s in scenario["steps"] if s["index"] == entry["source_index"])
                assert states == {name: name in expected for name in ("A", "B", "C")}
                row["service_states"] = states
            facts.append(row)
        assert actual.check_integrity().ok
    write_json(target / "execution-check.json", {"command": command, "cli_exit_code": code,
               "executed_requests": executed, "actual_model_calls": 0, "tools": facts})
    if prepared:
        write_json(target / "snapshot-evidence.json", snapshot_evidence(client, source, target / "record"))
    cleanup = check_resources(client, source, target)
    write_json(target / "cleanup.json", cleanup)
    return {"output": str(target), "cli_exit_code": code, "execution_status": report["execution_status"],
            "run_id": run.run_id, "actual_node_count": report["actual_node_count"],
            "completed_steps": report["completed_steps"], "incomplete_step": report["incomplete_step"],
            "actual_model_calls": 0, "historical_tools": expected_calls, "cleanup_confirmed": True}


def main(source, output, services_config=None):
    source, output = source.resolve(), output.resolve()
    source = source if (source / "catalog.sqlite").exists() else source / "record"
    outside_store(source.parent, output)
    if source.is_relative_to(output):
        raise ValueError("acceptance output cannot contain the source")
    output.mkdir(parents=True, exist_ok=False)
    summary = {"status": "failed", "source": str(source), "runs": [], "consistency_verification": "not-performed"}
    write_json(output / "acceptance.json", summary)
    before = fixture.store_hashes(source)
    write_json(output / "source-before.json", before)
    client = None
    try:
        scenario = json.loads((source.parent / "scenario.json").read_text())
        with RecordStore.open(source) as origin:
            selection = pin_replay(origin, "C20", "C40").provenance()
        write_json(output / "source-selection.json", selection)
        config = services_config.resolve() if services_config is not None else output / "services.json"
        if services_config is None:
            write_json(config, {"services": [
                {"name": name, "start_command": scenario["services"][name]["start_command"],
                 "readiness_command": scenario["services"][name]["request_command"], "timeout_sec": 30}
                for name in ("A", "B")
            ]})
        with RecordStore.open(source) as origin:
            plan = pin_replay(origin, "C20", "C40")
            selected = resolve_services(load_service_input(config), origin, plan)
        assert [service.name for service in selected.services] == ["A", "B"]
        client = connect_docker(600)
        containers = {c.id: c.status for c in client.containers.list(all=True)}
        for name, services in (("with-services", config), ("without-services", None)):
            summary["phase"] = name
            row = run_case(source, output / name, services, scenario, selection, client)
            summary["runs"].append(row)
            assert fixture.store_hashes(source) == before
            assert {c.id: c.status for c in client.containers.list(all=True)} == containers
            write_json(output / "acceptance.json", summary, replace=True)
        summary.update(status="passed", source_unchanged=True, cleanup_confirmed=True,
                       source_selection_sha256=selection["selection_sha256"],
                       unrelated_containers_unchanged=True, actual_model_calls=0)
    except BaseException as error:
        summary.update(error_type=type(error).__name__, error=str(error))
        raise
    finally:
        after = fixture.store_hashes(source)
        summary["source_unchanged"] = after == before
        write_json(output / "source-after.json", after)
        if client is not None:
            client.close()
        write_json(output / "acceptance.json", summary, replace=True)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="existing 40-step source run or Store")
    parser.add_argument("--services-config", type=Path, help="existing A/B configuration (C must be absent)")
    parser.add_argument("--output", required=True, type=Path, help="new acceptance directory")
    args = parser.parse_args()
    main(args.source, args.output, args.services_config)
