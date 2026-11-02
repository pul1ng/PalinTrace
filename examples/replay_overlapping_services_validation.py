"""Validate explicit A/B preparation and unchanged C21-C40 on the saved overlap source."""

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import replay_service_fixture as fixture

from palinreplay.cli import main as cli_main
from palinreplay.core.models import ArtifactRef, Node, Operation, Step
from palinreplay.record.restore import load_restore_materials
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.io import write_json
from palinreplay.storage.rootfs import SCOPE
from palinreplay.storage.store import RecordStore


def full_comparison(client, source, actual):
    """Inventory the whole captured scope; keep every residual path and field."""
    with RecordStore.open(source) as origin, RecordStore.open(actual) as current:
        left = load_restore_materials(origin, "C40")
        right = load_restore_materials(current, "C20")
        before = load_restore_materials(origin, "C20")
        initial = load_restore_materials(current, "C0")
    assert left.snapshot.location == right.snapshot.location
    script = """import importlib.util, json
from pathlib import Path
spec = importlib.util.spec_from_file_location("palin_worker", "/palin-btrfs-worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
roots = ROOTS
scope = SCOPE
left, right = [worker.inventory(Path(root), scope) for root in roots]
differences = []
for name in sorted(set(left["entries"]) | set(right["entries"])):
    a, b = left["entries"].get(name), right["entries"].get(name)
    if a != b:
        fields = [key for key in sorted(set(a or {}) | set(b or {}))
                  if (a or {}).get(key) != (b or {}).get(key)]
        differences.append({"path": name, "fields": fields, "source": a, "actual": b})
products = []
for root in roots:
    work = Path(root) / "testbed/.palin-service-demo"
    products.append({
        "report": json.loads((work / "report.json").read_text()),
        "results": {p.name: json.loads(p.read_text()) for p in sorted((work / "results").glob("*.json"))},
    })
identities = []
for root in INITIAL_ROOTS:
    paths = {name: Path(root) / "opt/palin-replay-service-fixture" / name / "service.json"
             for name in ("A", "B", "C")}
    identities.append({name: json.loads(path.read_text()) if path.exists() else None
                       for name, path in paths.items()})
print(json.dumps({
    "scope": scope["id"], "source_paths": len(left["entries"]), "actual_paths": len(right["entries"]),
    "strict": worker.compare(left, right), "differences": differences,
    "source_hardlink_groups": left["hardlink_groups"], "actual_hardlink_groups": right["hardlink_groups"],
    "hardlinks_equal": left["hardlink_groups"] == right["hardlink_groups"],
    "business_products_equal": products[0] == products[1],
    "source_products": products[0], "actual_products": products[1],
    "source_initial_identities": identities[0], "prepared_initial_identities": identities[1],
}))
""".replace("INITIAL_ROOTS", repr(["/pool/" + item.snapshot.name for item in (before, initial)]))
    script = script.replace("ROOTS", repr(["/pool/" + item.snapshot.name for item in (left, right)]))
    script = script.replace("scope = SCOPE", "scope = " + repr(SCOPE))
    with BtrfsPool(client, left.snapshot.location) as pool:
        for item in (left, right, before, initial):
            pool.verify(item.snapshot)
        result = pool.container.exec_run(["python3", "-B", "-c", script])
        assert result.exit_code == 0, result.output.decode(errors="replace")[-2000:]
        return json.loads(result.output)


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
            "python3", "-B", "-c", "from pathlib import Path; assert not Path('/pool', "
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


def main(source, output):
    source, output = source.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    summary = {"status": "failed", "source": str(source / "record")}
    write_json(output / "acceptance.json", summary)
    before = fixture.store_hashes(source)
    scenario = json.loads((source / "scenario.json").read_text())
    config = output / "services.json"
    # Human-selected A then B. C is deliberately absent from preparation.
    write_json(config, {"services": [
        {"name": name, "start_command": scenario["services"][name]["start_command"],
         "readiness_command": scenario["services"][name]["request_command"], "timeout_sec": 30}
        for name in ("A", "B")
    ]})
    client = connect_docker(600)
    try:
        containers = {c.id: c.status for c in client.containers.list(all=True)}
        replay = output / "replay"
        executed = []
        execute = RestoredExecutor.execute

        def observe(executor, request):
            assert json.loads((replay / "preparation.json").read_text())["status"] == "ready"
            executed.append(request)
            return execute(executor, request)

        with fixture.no_model_calls() as models, patch.object(RestoredExecutor, "execute", observe):
            code = cli_main(["replay", str(source / "record"), "--from", "C20", "--to", "C40",
                             "--services-config", str(config), "--validation", "lenient",
                             "--output", str(replay), "--progress", "plain"])
            assert models.call_count == 0
        report = json.loads((replay / "report.json").read_text())
        assert code == 2 and report["execution_status"] == "completed", report
        assert report["validation_status"] == "difference" and report["cleanup_confirmed"]
        assert report["r1"]["status"] == "passed" and report["r1"]["files"]["equal"]
        services = report["preparation"]["services"]
        assert report["preparation"]["status"] == "ready"
        assert [service["name"] for service in services] == ["A", "B"]
        assert all(service["status"] == "ready" and service["startup"]["exit_code"] == 0
                   and service["readiness"][-1]["exit_code"] == 0 for service in services)
        assert report["first_difference"]["difference_reason"] == "service-preparation"
        assert [mapping["source_index"] for mapping in report["mappings"]] == list(range(20, 41))
        assert all(step["starting_divergent"] for step in report["steps"])
        assert all(tool["equal"] for step in report["steps"] for tool in step["tools"])
        stats = report["statistics"]
        assert stats["actual_tool_executions"] == stats["historical_answer_supplies"] == 20
        assert stats["actual_model_calls"] == stats["actual_model_cost"] == 0
        tools = []
        with RecordStore.open(source / "record") as origin, RecordStore.open(replay / "record") as actual:
            c20 = load_restore_materials(origin, "C20")
            c0 = load_restore_materials(actual, "C0")
            assert c0.run.run_id != c20.run.run_id and c0.run.source_node_id is None
            assert c0.context["messages"] == c20.context["messages"]
            assert c0.runtime.source_container_id != c20.runtime.source_container_id
            assert c0.config["replay"]["source"] == report["source"]
            assert actual.read_json(ArtifactRef.model_validate(c0.config["replay"]["preparation"]["evidence"])) == report["preparation"]
            assert len(actual.list(Node)) == 21 and len(actual.list(Step)) == 20
            assert len(actual.list(Operation)) == 40
            for i, mapping in enumerate(report["mappings"][1:]):
                index = mapping["source_index"]
                original = fixture.tool_fact(origin, load_restore_materials(origin, f"C{index}").node)
                observed = fixture.tool_fact(actual, actual.get(Node, mapping["node_id"]))
                assert observed["request"] == original["request"] == executed[i]
                assert observed["exit_code"] == original["exit_code"] == 0 and observed["definitive"]
                assert observed["output"] == original["output"]
                states = json.loads(next(line.removeprefix("PALIN_SERVICE_STATES ")
                                         for line in observed["output"].splitlines()
                                         if line.startswith("PALIN_SERVICE_STATES ")))
                expected = next(step["expected_alive"] for step in scenario["steps"] if step["index"] == index)
                assert states == {name: name in expected for name in ("A", "B", "C")}
                tools.append({"source_index": index, "request": observed["request"],
                              "exit_code": observed["exit_code"], "output": observed["output"],
                              "service_states": states, "equal": True})
        write_json(output / "history-check.json", {"tools": tools, "all_equal": True})
        print("History passed; comparing full final filesystems and business products.", flush=True)
        compared = full_comparison(client, source / "record", replay / "record")
        write_json(output / "final-comparison.json", compared)
        assert compared["business_products_equal"]
        assert compared["actual_products"]["report"] == {"final_total": 56, "results": 8}
        assert len(compared["actual_products"]["results"]) == 8
        for name in ("A", "B"):
            assert compared["prepared_initial_identities"][name]
            assert compared["prepared_initial_identities"][name] != compared["source_initial_identities"][name]
        assert compared["prepared_initial_identities"]["C"] is None
        cleanup = check_resources(client, source / "record", replay)
        write_json(output / "cleanup.json", cleanup)
        assert fixture.store_hashes(source) == before
        assert {c.id: c.status for c in client.containers.list(all=True)} == containers
        summary.update(status="passed", replay_run_id=report["run_id"], cli_exit_code=code,
                       execution_status=report["execution_status"], validation_status=report["validation_status"],
                       r1_passed=True, prepared_services=["A", "B"], historical_tools=20,
                       all_tool_results_and_requests_equal=True, all_service_states_equal=True,
                       business_results=8, final_report=compared["actual_products"]["report"],
                       full_scope_paths=compared["actual_paths"], strict_final_equal=compared["strict"]["equal"],
                       residual_paths=len(compared["differences"]),
                       residual_fields=sorted({field for item in compared["differences"] for field in item["fields"]}),
                       hardlinks_equal=compared["hardlinks_equal"], source_unchanged=True,
                       cleanup_confirmed=True, unrelated_containers_unchanged=True,
                       actual_model_calls=0, actual_model_cost=0)
    except BaseException as error:
        summary.update(error_type=type(error).__name__, error=str(error))
        raise
    finally:
        client.close()
        write_json(output / "acceptance.json", summary, replace=True)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="saved 40-step source directory")
    parser.add_argument("--output", required=True, type=Path, help="new acceptance directory")
    args = parser.parse_args()
    main(args.source, args.output)
