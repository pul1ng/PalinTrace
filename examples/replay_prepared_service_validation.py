"""Compare missing/prepared service Replay on an existing, unchanged C2-C4 source."""

import argparse
import base64
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
from palinreplay.storage.store import RecordStore


def main(source, output):
    source, output = source.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    summary = {"status": "failed", "source": str(source)}
    write_json(output / "acceptance.json", summary)
    before = fixture.store_hashes(source / "record")
    client = connect_docker(600)
    try:
        containers = {c.id: c.status for c in client.containers.list(all=True)}
        baseline, prepared = output / "without-services", output / "with-services"
        fixture.replay_once(source, baseline, client)
        fixture.check_r1(source, baseline, client)
        missing = fixture.check_difference(source, baseline)
        assert missing["status"] == "passed"
        config = output / "services.json"
        write_json(config, {"services": [{
            "name": "fixture", "start_command": fixture.START_COMMAND,
            "readiness_command": fixture.REQUEST_COMMAND, "timeout_sec": 30,
        }]})
        executed = []
        execute = RestoredExecutor.execute

        def observe(executor, request):
            assert json.loads((prepared / "preparation.json").read_text())["status"] == "ready"
            executed.append(request)
            return execute(executor, request)

        with fixture.no_model_calls() as models, patch.object(RestoredExecutor, "execute", observe):
            code = cli_main(["replay", str(source / "record"), "--from", "C2", "--to", "C4",
                             "--services-config", str(config), "--validation", "lenient",
                             "--output", str(prepared), "--progress", "plain"])
            assert models.call_count == 0
        report = json.loads((prepared / "report.json").read_text())
        assert code == 2 and report["execution_status"] == "completed", report
        assert report["validation_status"] == "difference" and report["cleanup_confirmed"]
        assert report["r1"]["status"] == "passed" and report["r1"]["files"]["equal"]
        journal = report["preparation"]
        assert journal["status"] == journal["services"][0]["status"] == "ready"
        service = journal["services"][0]
        assert service["startup"]["exit_code"] == 0
        assert base64.b64decode(service["startup"]["output_base64"]) == b"PALIN_SERVICE_READY\n"
        probe = service["readiness"][-1]
        assert probe["exit_code"] == 0
        assert json.loads(base64.b64decode(probe["output_base64"])) == {"status": 200, "body": fixture.BODY}
        assert report["first_difference"]["difference_reason"] == "service-preparation"
        assert "opt/palin-replay-service-fixture/service.json" in report["preparation_files"]["different_paths"]
        assert [mapping["source_index"] for mapping in report["mappings"]] == [2, 3, 4]
        assert all(step["starting_divergent"] for step in report["steps"])
        stats = report["statistics"]
        assert stats["actual_tool_executions"] == stats["historical_answer_supplies"] == 2
        assert stats["actual_model_calls"] == stats["actual_model_cost"] == 0
        facts = []
        with RecordStore.open(source / "record") as origin, RecordStore.open(prepared / "record") as current:
            c2 = load_restore_materials(origin, "C2")
            c0 = load_restore_materials(current, "C0")
            final = load_restore_materials(current, "C2")
            assert c0.run.run_id != c2.run.run_id and c0.run.source_node_id is None
            assert c0.context["messages"] == c2.context["messages"]
            assert len(c0.context["messages"]) < len(load_restore_materials(origin, "C3").context["messages"])
            assert c0.config["replay"]["source"] == report["source"]
            assert current.read_json(ArtifactRef.model_validate(c0.config["replay"]["preparation"]["evidence"])) == journal
            assert c0.runtime.source_container_id != c2.runtime.source_container_id
            assert len(current.list(Node)) == 3 and len(current.list(Step)) == 2
            assert len(current.list(Operation)) == 4
            for index, mapping in enumerate(report["mappings"][1:]):
                original = fixture.tool_fact(origin, load_restore_materials(origin, f"C{mapping['source_index']}").node)
                actual = fixture.tool_fact(current, current.get(Node, mapping["node_id"]))
                assert actual["request"] == original["request"] == executed[index]
                assert actual["exit_code"] == original["exit_code"] == 0
                assert actual["output"] == original["output"] and actual["definitive"]
                facts.append(actual)
            with BtrfsPool(client, c0.snapshot.location) as pool:
                roots = ["/pool/" + item.snapshot.name for item in (c2, c0, final)]
                script = ("import json; from pathlib import Path; roots=" + repr(roots) + "; "
                          "p='opt/palin-replay-service-fixture/service.json'; "
                          "print(json.dumps([json.loads((Path(root)/p).read_text()) "
                          "if (Path(root)/p).exists() else None for root in roots]))")
                result = pool.container.exec_run(["python3", "-B", "-c", script])
                assert result.exit_code == 0, result.output
                old_identity, new_identity, stopped_identity = json.loads(result.output)
                assert new_identity and new_identity != old_identity and stopped_identity is None
        cleanup = fixture.check_resources(source, [baseline, prepared], client)
        write_json(output / "cleanup.json", cleanup)
        assert fixture.store_hashes(source / "record") == before
        assert {c.id: c.status for c in client.containers.list(all=True)} == containers
        summary.update(status="passed", missing_service_verified=True, prepared_cli_exit_code=code,
                       prepared_run_id=report["run_id"], prepared_identity=new_identity,
                       source_selection_sha256=report["source"]["selection_sha256"],
                       historical_tools=facts, r1_passed=True, preparation_ready=True,
                       C4_stopped_service=True, source_unchanged=True, cleanup_confirmed=True,
                       unrelated_containers_unchanged=True, actual_model_calls=0,
                       validation_status=report["validation_status"])
    except BaseException as error:
        summary.update(error_type=type(error).__name__, error=str(error))
        raise
    finally:
        client.close()
        write_json(output / "acceptance.json", summary, replace=True)
    print("PASS: same-source missing/prepared service contrast, real C3 and C4, source and cleanup")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="saved single-service source directory")
    parser.add_argument("--output", required=True, type=Path, help="new acceptance directory")
    args = parser.parse_args()
    main(args.source, args.output)
