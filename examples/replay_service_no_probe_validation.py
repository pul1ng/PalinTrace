"""Real Record/preprocess/Replay acceptance for binary launch intent without probes.

Fixed analysis responses are an explicit oracle; --llm-config uses the real
preprocess CLI/model on the same source instead. Tool results are never mocked.
"""

import argparse
import hashlib
import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import replay_service_fixture as fixture

from palinreplay.cli import main as cli_main
from palinreplay.core.models import Node
from palinreplay.preprocess.config import PreprocessConfig
from palinreplay.preprocess.manifest import load_manifest, manifest_document, select_services
from palinreplay.preprocess.pipeline import run_preprocess
from palinreplay.record.restore import load_restore_materials
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.io import write_json
from palinreplay.storage.store import RecordStore

START = "./start-A.sh >a.log 2>&1 &"
FAILED_START = "./start-A.sh --fail"
STOP = "./start-A.sh --stop"
FINISH = "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\n'"
CASES = ("no-probe", "failed-start", "stop-C3", "files-only")

SERVER = '''import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
root = Path(__file__).parent
class Handler(BaseHTTPRequestHandler):
    def request(self):
        with (root / "requests.log").open("a") as log:
            log.write(self.command + " " + self.path + "\\n")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    do_GET = do_POST = request
    def log_message(self, *args):
        pass
server = HTTPServer(("127.0.0.1", 18765), Handler)
fields = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
(root / "service.json").write_text(json.dumps({"pid": os.getpid(), "start": fields[19]}))
server.serve_forever()
'''
STOPPER = '''import json, os, signal, time
from pathlib import Path
root = Path(__file__).parent
identity = root / "service.json"
saved = json.loads(identity.read_text())
def current():
    try:
        return Path("/proc/{}/stat".format(saved["pid"])).read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None
fields = current()
assert fields and fields[19] == saved["start"] and fields[0] != "Z"
os.kill(saved["pid"], signal.SIGTERM)
deadline = time.monotonic() + 5
while True:
    fields = current()
    if fields is None or fields[0] == "Z":
        break
    if time.monotonic() >= deadline:
        raise TimeoutError("stop did not complete")
    time.sleep(0.01)
identity.unlink()
print("A_STOPPED")
'''
STARTER = f'''#!/bin/bash
if [ "$1" = "--fail" ]; then
  printf 'A_LAUNCH_FAILED\\n' >&2
  exit 17
fi
if [ "$1" = "--stop" ]; then
  exec {fixture.PYTHON} -B {fixture.DIRECTORY}/stop.py
fi
printf 'start\\n' >> {fixture.DIRECTORY}/starts.log
exec {fixture.PYTHON} -B {fixture.DIRECTORY}/server.py
'''


def files():
    return {"server.py": SERVER, "stop.py": STOPPER}


def initial_command():
    return fixture.python_command(
        "from pathlib import Path; "
        f"root = Path({fixture.DIRECTORY!r}); root.mkdir(); "
        f"[(root / name).write_text(body) for name, body in {files()!r}.items()]; "
        "(root / 'requests.log').touch(); (root / 'starts.log').touch(); "
        f"script = Path('start-A.sh'); script.write_text({STARTER!r}); script.chmod(0o755)"
    )


def commands(case):
    first = {"no-probe": START, "stop-C3": START, "failed-start": FAILED_START,
             "files-only": "printf 'plain file\\n' > first.txt"}[case]
    last = FINISH + (" && " + STOP if case == "stop-C3" else " && cp c2.txt c3.txt")
    return [first, "printf 'ordinary file operation\\n' > c2.txt", last]


class FixtureAnalysis:
    """One fixed response per chunk; real tool results remain unchanged."""

    def __init__(self, case):
        self.case = case

    def complete(self, messages, **parameters):
        payload = json.loads(messages[1]["content"].split("\n\n", 1)[1])
        events = []
        for call in payload["calls"]:
            node = call["node_index"]
            assert call["request"]["command"] == commands(self.case)[node - 1]
            if node == 1 and self.case != "files-only":
                action, command, quote = "start", commands(self.case)[0], None
            elif node == 3 and self.case == "stop-C3":
                action, command, quote = "stop", STOP, "A_STOPPED"
            else:
                continue
            events.append({
                "name": "A", "service_key": "./start-A.sh",
                "node_index": node, "execution_id": call["execution_id"],
                "action": action, "command": command, "output_quote": quote,
                "action_result": "succeeded" if action == "stop" else "unattributed",
            })
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"events": events}), refusal=None), finish_reason="stop")], usage=None)

def preprocess(source, output, run_id, case, llm_config=None):
    if llm_config:
        assert cli_main(["preprocess", str(source / "record"), "--run", run_id,
                         "--llm-config", str(llm_config), "--output", str(output)]) == 0
        return "live-model"
    run_preprocess(source / "record", run_id=run_id, output=output, config=PreprocessConfig(
        llm={"base_url": "http://fixture.invalid/v1", "api_key": "unused",
             "model": "controlled-no-probe-evidence"},
        preprocess={"tokenizer_model": "gpt-4o"}),
        client=FixtureAnalysis(case))
    return "fixed-analysis-oracle"


def check_manifest(path, case):
    manifest = load_manifest(path)
    expected = [] if case == "files-only" else [("A", 1, 3 if case == "stop-C3" else 4)]
    assert [(s.name, s.start_inclusive, s.end_exclusive) for s in manifest.services] == expected
    raw = json.loads(path.read_text())
    assert "unknowns" not in raw
    for service in raw["services"]:
        assert service["launch"]["start_command"] == (FAILED_START if case == "failed-start" else START)
        assert service["launch"]["cwd"] == "/testbed"
        assert "readiness_command" not in service["launch"]
        assert "probe" not in service["evidence"]
    selections = {str(node): [s.name for s in select_services(manifest, node)] for node in range(4)}
    assert selections == {str(node): (["A"] if expected and 1 <= node < expected[0][2] else [])
                          for node in range(4)}
    return manifest, selections


def snapshot_rows(client, source, replay):
    materials, labels = [], []
    for prefix, directory in (("source", source), ("actual", replay)):
        if directory is None:
            continue
        with RecordStore.open(directory / "record") as store:
            for node in sorted(store.list(Node), key=lambda node: node.index):
                materials.append(load_restore_materials(store, node.node_id))
                labels.append(f"{prefix}_C{node.index}")
    script = '''import json, sys
from pathlib import Path
rows = []
for path in sys.argv[1:]:
    root = Path(path)
    identity = root / "service.json"
    rows.append({"requests": (root / "requests.log").read_text().splitlines(),
                 "starts": (root / "starts.log").read_text().splitlines(),
                 "identity": json.loads(identity.read_text()) if identity.exists() else None})
print(json.dumps(rows))
'''
    with BtrfsPool(client, materials[0].snapshot.location) as pool:
        for material in materials:
            pool.verify(material.snapshot)
        result = pool.container.exec_run(["python3", "-I", "-c", script,
            *["/pool/" + m.snapshot.name + fixture.DIRECTORY for m in materials]])
        assert result.exit_code == 0, result.output
    return dict(zip(labels, json.loads(result.output), strict=True))


def replay_once(source, output, manifest_path, run_id, case, *, start="C1"):
    historical, preparation = [], []
    execute, execute_command = RestoredExecutor.execute, RestoredExecutor.exec

    def observe(executor, request):
        journal = json.loads((output / "preparation.json").read_text())
        assert journal["status"] == "ready"
        historical.append(request)
        return execute(executor, request)

    def observe_command(executor, command, **kwargs):
        # exec is also used by history; the persisted journal gives the phase.
        journal = json.loads((output / "preparation.json").read_text())
        if journal["status"] == "preparing":
            preparation.append(command)
        return execute_command(executor, command, **kwargs)

    args = ["replay", str(source / "record"), "--run", run_id, "--from", start, "--to", "C3",
            "--services-config", str(manifest_path), "--output", str(output), "--progress", "plain"]
    stdout, stderr = StringIO(), StringIO()
    with fixture.no_model_calls(), patch.object(RestoredExecutor, "execute", observe), \
            patch.object(RestoredExecutor, "exec", observe_command), \
            redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli_main(args)
    (output / "cli.stdout.txt").write_text(stdout.getvalue())
    (output / "cli.stderr.txt").write_text(stderr.getvalue())
    report = json.loads((output / "report.json").read_text())
    assert report["r1"]["status"] == "passed" and report["r1"]["files"]["equal"]
    assert report["cleanup_confirmed"] and not report["cleanup_errors"]
    active = case != "files-only" and start == "C1"
    assert report["services_manifest"]["selected"] == (["A"] if active else [])
    expected_start = FAILED_START if case == "failed-start" else START
    assert preparation == ([expected_start] if active else []), preparation
    journal = report["preparation"]
    evidence = {"cli_args": args, "cli_exit_code": code, "run_id": report.get("run_id"),
                "selected": report["services_manifest"]["selected"], "preparation_commands": preparation,
                "historical_tools": [], "execution_status": report["execution_status"], "r1_passed": True}
    if active and case == "failed-start":
        assert code == 1 and report["execution_status"] == "failed", report
        assert report["phase"] == "service-preparation" and journal["status"] == "failed"
        service, = journal["services"]
        assert service["startup"]["exit_code"] == 17
        assert historical == [] and report["statistics"]["actual_tool_executions"] == 0
        assert not (output / "record").exists()
        write_json(output / "execution-check.json", evidence)
        return evidence
    assert code == 0 and report["execution_status"] == "completed", report.get("error")
    assert journal["status"] == "ready"
    for service in journal["services"]:
        assert service["status"] == "ready" and service["startup"]["exit_code"] == 0
        assert "readiness" not in service and "final_readiness" not in service
    indexes = list(range(int(start[1:]) + 1, 4))
    assert [m["source_index"] for m in report["mappings"]] == indexes
    assert len(historical) == report["statistics"]["actual_tool_executions"] == len(indexes)
    assert report["statistics"]["actual_model_calls"] == 0
    with RecordStore.open(source / "record") as origin, RecordStore.open(output / "record") as actual:
        for mapping, request in zip(report["mappings"], historical, strict=True):
            old = fixture.tool_fact(origin, load_restore_materials(origin, f"C{mapping['source_index']}").node)
            new = fixture.tool_fact(actual, actual.get(Node, mapping["node_id"]))
            assert old["request"] == new["request"] == request
            assert old["definitive"] and new["definitive"]
            assert old["exit_code"] == new["exit_code"] == 0
            assert old["output"] == new["output"]
            evidence["historical_tools"].append(new)
    write_json(output / "execution-check.json", evidence)
    return evidence


def main(preload, output, llm_config=None):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    summary = {"status": "failed", "cases": [], "output": str(output)}
    client = connect_docker(600)
    containers = {c.id: c.status for c in client.containers.list(all=True)}
    write_json(output / "acceptance.json", summary)
    try:
        for case in CASES:
            target = output / case
            target.mkdir()
            source = target / "source"
            summary.update(phase=f"{case}:record")
            write_json(output / "acceptance.json", summary, replace=True)
            recording = fixture.record_source(preload, source, client, decisions=commands(case),
                files=files(), initial_command=initial_command(),
                expected_exit_codes=[17 if case == "failed-start" else 0, 0, 0])
            if case == "no-probe":
                assert recording["tools"][0]["output"] == ""
            before = fixture.store_hashes(source / "record")
            write_json(source / "identity.json", {"store_hashes": before})
            summary["phase"] = f"{case}:preprocess"
            write_json(output / "acceptance.json", summary, replace=True)
            mode = preprocess(source, target / "preprocess", recording["run_id"], case, llm_config)
            path = target / "preprocess/services.json"
            manifest, selections = check_manifest(path, case)
            summary["phase"] = f"{case}:replay"
            write_json(output / "acceptance.json", summary, replace=True)
            replays = []
            evidence = replay_once(source, target / "replay", path, recording["run_id"], case)
            replays.append(target / "replay")
            rows = snapshot_rows(client, source, None if case == "failed-start" else target / "replay")
            write_json(target / "snapshot-check.json", rows)
            assert all(row["requests"] == [] for row in rows.values()), rows
            if case in ("no-probe", "stop-C3"):
                assert rows["source_C3"]["starts"] == ["start"]
                # C1 may precede the background child writing anything. Replay
                # restores exactly that snapshot and appends one startup.
                assert rows["actual_C3"]["starts"] == rows["source_C1"]["starts"] + ["start"]
                assert bool(rows["source_C3"]["identity"]) == (case == "no-probe")
                assert bool(rows["actual_C3"]["identity"]) == (case == "no-probe")
                if case == "no-probe":
                    assert rows["actual_C3"]["identity"] != rows["source_C3"]["identity"]
            else:
                assert all(not row["starts"] and row["identity"] is None for row in rows.values())
            # Replay from the stop boundary itself selects no service and runs no history.
            boundary = None
            if case == "stop-C3":
                boundary = replay_once(source, target / "replay-C3", path,
                                       recording["run_id"], case, start="C3")
                replays.append(target / "replay-C3")
            cleanup = fixture.check_resources(source, replays, client)
            write_json(target / "cleanup.json", cleanup)
            assert fixture.store_hashes(source / "record") == before
            summary["cases"].append({"case": case, "source_run_id": recording["run_id"],
                "source": str(source / "record"), "preprocess_mode": mode,
                "manifest": manifest_document(manifest), "selections": selections,
                "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "replay": evidence, "boundary_replay": boundary, "business_requests": 0,
                "source_unchanged": True, "cleanup_confirmed": True})
            write_json(output / "acceptance.json", summary, replace=True)
        assert {c.id: c.status for c in client.containers.list(all=True)} == containers
        summary.update(status="passed", phase="finished", unrelated_containers_unchanged=True)
    except BaseException as error:
        summary.update(error_type=type(error).__name__, error=str(error))
        raise
    finally:
        client.close()
        write_json(output / "acceptance.json", summary, replace=True)
    print(f"PASS: four real Record/preprocess/Replay cases without probes -> {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--llm-config", type=Path, help="real model instead of fixed analysis responses")
    args = parser.parse_args()
    main(args.preload, args.output, args.llm_config)
