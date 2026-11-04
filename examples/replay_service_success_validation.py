"""Record and replay C1 start A, C2 one successful request, C3 stop A.

Uses the existing real-container recorder, preprocessing pipeline and Replay CLI.
Only agent decisions are fixed; tool results and snapshots are real. Readiness
uses a child-process pipe and TCP connect, never an HTTP business request.
By default, explicit fixture analysis responses are checked against the real
Record by preprocess; --llm-config opts into live model analysis instead.
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
from palinreplay.core.models import Node, Operation, Step
from palinreplay.preprocess.config import PreprocessConfig
from palinreplay.preprocess.manifest import load_manifest, select_services
from palinreplay.preprocess.pipeline import run_preprocess
from palinreplay.record.restore import load_restore_materials
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.replay.plan import pin_replay
from palinreplay.replay.service_config import load_service_input, resolve_services
from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.io import write_json
from palinreplay.storage.store import RecordStore

READY_COMMAND = f"{fixture.PYTHON} -B {fixture.DIRECTORY}/lifecycle.py ready"

SERVER = '''import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

root = Path(__file__).parent
config = json.loads((root / "config.json").read_text())
log = root / "requests.log"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with log.open("a") as handle:
            handle.write("GET " + self.path + "\\n")
        body = config["body"].encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass

server = HTTPServer((config["host"], config["port"]), Handler)
log.touch(exist_ok=True)
# HTTPServer has bound and entered listen(); no HTTP request is needed to start.
ready_fd = int(sys.argv[1])
os.write(ready_fd, b"READY\\n")
os.close(ready_fd)
server.serve_forever()
'''

LIFECYCLE = '''import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

root = Path(__file__).parent
config = json.loads((root / "config.json").read_text())
identity = root / "service.json"

def process(pid):
    try:
        fields = Path("/proc/{}/stat".format(pid)).read_text().rsplit(")", 1)[1].split()
        return {"state": fields[0], "start": fields[19]}
    except FileNotFoundError:
        return None

def port_closed():
    try:
        with socket.create_connection((config["host"], config["port"]), timeout=0.5):
            return False
    except ConnectionRefusedError:
        return True

def running():
    saved = json.loads(identity.read_text())
    current = process(saved["pid"])
    if current is None or current["start"] != saved["start"] or current["state"] == "Z":
        raise RuntimeError("this fixture service is not running")
    return saved

def requests():
    return len((root / "requests.log").read_text().splitlines())

if sys.argv[1] == "start":
    if not port_closed():
        raise RuntimeError("fixture port already in use")
    read_fd, write_fd = os.pipe()
    try:
        child = subprocess.Popen([sys.executable, "-B", str(root / "server.py"), str(write_fd)],
                                 pass_fds=(write_fd,), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
    finally:
        os.close(write_fd)
    try:
        if not select.select([read_fd], [], [], 10)[0]:
            raise TimeoutError("service readiness notification deadline exceeded")
        if os.read(read_fd, 64) != b"READY\\n" or child.poll() is not None:
            raise RuntimeError("service exited before readiness")
        identity.write_text(json.dumps({"pid": child.pid, "start": process(child.pid)["start"]}))
    except BaseException:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
        raise
    finally:
        os.close(read_fd)
    print("PALIN_SERVICE_A_STARTED")
elif sys.argv[1] == "ready":
    running()
    if port_closed():
        raise RuntimeError("fixture is not listening")
    print("PALIN_SERVICE_A_READY requests={}".format(requests()))
elif sys.argv[1] == "stop":
    saved = running()
    os.kill(saved["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 5
    while True:
        current = process(saved["pid"])
        exited = current is None or current["state"] == "Z"
        if exited and port_closed():
            identity.unlink()
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("service shutdown unconfirmed")
        time.sleep(0.05)
    print("PALIN_SERVICE_A_STOPPED requests={}".format(requests()))
else:
    raise ValueError("expected start, ready or stop")
'''


def fixture_files():
    return {**fixture.fixture_files(), "server.py": SERVER, "lifecycle.py": LIFECYCLE,
            "config.json": json.dumps({**fixture.CONFIG, "name": "A"}, sort_keys=True) + "\n"}


def commands():
    install = fixture.python_command(
        "from pathlib import Path; "
        f"root = Path({fixture.DIRECTORY!r}); root.mkdir(); "
        f"[(root / name).write_text(body) for name, body in {fixture_files()!r}.items()]"
    )
    return [
        install + " && " + fixture.START_COMMAND + " && " + READY_COMMAND,
        fixture.REQUEST_COMMAND,
        "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\n' && " + fixture.STOP_COMMAND,
    ]


class FixtureAnalysis:
    """Single-pass extraction oracle using the recorded fixture commands."""

    def complete(self, messages, **parameters):
        payload = json.loads(messages[1]["content"].split("\n\n", 1)[1])
        events = []
        for call in payload["calls"]:
            index = call["node_index"]
            assert call["request"]["command"] == commands()[index - 1]
            if index == 1:
                action, command, quote = "start", fixture.START_COMMAND, "PALIN_SERVICE_A_STARTED"
            elif index == 3:
                action, command, quote = "stop", fixture.STOP_COMMAND, "PALIN_SERVICE_A_STOPPED requests=1"
            else:
                continue
            events.append({
                "name": "A", "service_key": fixture.DIRECTORY + "/lifecycle.py",
                "node_index": index, "execution_id": call["execution_id"],
                "action": action, "command": command,
                "action_result": "succeeded", "output_quote": quote,
            })
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({"events": events}), refusal=None), finish_reason="stop")],
            usage=None,
        )

def preprocess_source(source, output, run_id, llm_config=None):
    if llm_config is not None:
        command = ["preprocess", str(source / "record"), "--run", run_id,
                   "--llm-config", str(llm_config), "--output", str(output)]
        assert cli_main(command) == 0, "preprocess CLI failed"
        return "live-model"
    config = PreprocessConfig(
        llm={"base_url": "http://fixture.invalid/v1", "model": "controlled-service-evidence",
             "api_key": "unused"},
        preprocess={"tokenizer_model": "gpt-4o"},
    )
    run_preprocess(source / "record", run_id=run_id, config=config, output=output,
                   client=FixtureAnalysis())
    return "controlled-evidence-oracle"


def snapshot_evidence(client, source, actual=None):
    """Read retained snapshots rather than inferring request counts from commands."""
    materials, labels = [], []
    for prefix, path, indexes in (("source", source / "record", (1, 2, 3)),
                                  ("actual", actual / "record" if actual else None, (2, 3))):
        if path is None:
            continue
        with RecordStore.open(path) as store:
            for index in indexes:
                materials.append(load_restore_materials(store, f"C{index}"))
                labels.append(f"{prefix}_C{index}")
    script = '''import json, sys
from pathlib import Path
rows = []
for path in sys.argv[1:]:
    root = Path(path)
    log = (root / "requests.log").read_text().splitlines()
    identity = root / "service.json"
    rows.append({"requests": log,
                 "identity": json.loads(identity.read_text()) if identity.exists() else None})
print(json.dumps(rows))
'''
    with BtrfsPool(client, materials[0].snapshot.location) as pool:
        for item in materials:
            pool.verify(item.snapshot)
        result = pool.container.exec_run([
            "python3", "-I", "-c", script,
            *["/pool/" + item.snapshot.name + fixture.DIRECTORY for item in materials],
        ])
        assert result.exit_code == 0, result.output
    rows = dict(zip(labels, json.loads(result.output), strict=True))
    check_snapshot_rows(rows, replay=actual is not None)
    return rows


def check_snapshot_rows(rows, *, replay):
    """Reject hidden readiness GETs, missing business requests and unsuccessful stops."""
    assert rows["source_C1"]["requests"] == []
    for label, row in rows.items():
        if label != "source_C1":
            assert row["requests"] == ["GET /"], (label, row)
        assert bool(row["identity"]) == (not label.endswith("C3")), (label, row)
    if replay:
        assert rows["actual_C2"]["identity"] != rows["source_C1"]["identity"]


def verify_manifest(source, path, run_id):
    manifest = load_manifest(path)
    assert manifest.source.run_id == run_id
    assert manifest.source.start_index == 0 and manifest.source.end_index == 3
    assert [(s.name, s.start_inclusive, s.end_exclusive) for s in manifest.services] == [("A", 1, 3)]
    assert not manifest.unknowns
    service, = manifest.services
    assert service.launch is not None
    assert service.launch.start_command == fixture.START_COMMAND
    assert service.launch.readiness_command is None
    assert service.evidence.start.index == service.evidence.probe.index == 1
    assert service.evidence.stop is not None and service.evidence.stop.index == 3
    assert [s.name for s in select_services(manifest, 1)] == ["A"]
    assert not select_services(manifest, 0) and not select_services(manifest, 3)
    with RecordStore.open(source / "record") as store:
        plan = pin_replay(store, "C1", "C3", run_id=run_id)
        selected = resolve_services(load_service_input(path), store, plan)
        assert [s.name for s in selected.services] == ["A"]
        assert service.launch.cwd == load_restore_materials(store, "C1").runtime.launch.workdir
    return manifest


def replay_once(source, output, manifest, run_id):
    executed, prepared_counts = [], []
    execute = RestoredExecutor.execute

    def observe(executor, request):
        if manifest is not None:
            journal = json.loads((output / "preparation.json").read_text())
            assert journal["status"] == "ready"
            if not executed:
                check = executor.exec(fixture.python_command(
                    "from pathlib import Path; "
                    f"print(len(Path({fixture.DIRECTORY + '/requests.log'!r}).read_text().splitlines()))"
                ))
                assert check.exit_code == 0 and check.output == b"0\n"
                prepared_counts.append(0)
        executed.append(request)
        return execute(executor, request)

    command = ["replay", str(source / "record"), "--from", "C1", "--to", "C3",
               "--run", run_id, "--output", str(output), "--progress", "plain"]
    if manifest is not None:
        command += ["--services-config", str(manifest)]
    stdout, stderr = StringIO(), StringIO()
    with fixture.no_model_calls(), patch.object(RestoredExecutor, "execute", observe), \
            redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli_main(command)
    (output / "cli.stdout.txt").write_text(stdout.getvalue())
    (output / "cli.stderr.txt").write_text(stderr.getvalue())
    report = json.loads((output / "report.json").read_text())
    prepared = manifest is not None
    assert code == (0 if prepared else 2), report.get("error")
    assert report["execution_status"] == ("completed" if prepared else "stopped")
    assert report["source_start"]["index"] == 1
    assert report["r1"]["status"] == "passed" and report["r1"]["files"]["equal"]
    assert report["cleanup_confirmed"] and not report["cleanup_errors"]
    assert report["execution_policy"] == "historical-exit-code-v1"
    indexes = [2, 3] if prepared else [2]
    assert [m["source_index"] for m in report["mappings"]] == indexes
    assert len(executed) == len(indexes)
    assert report["statistics"]["actual_tool_executions"] == len(indexes)
    assert report["statistics"]["historical_answer_supplies"] == len(indexes)
    assert report["statistics"]["actual_model_calls"] == 0
    facts = []
    with RecordStore.open(source / "record") as origin, RecordStore.open(output / "record") as actual:
        assert sorted(n.index for n in actual.list(Node)) == indexes
        assert len(actual.list(Step)) == len(indexes)
        assert len(actual.list(Operation)) == 2 * len(indexes)
        for mapping, request in zip(report["mappings"], executed, strict=True):
            original = fixture.tool_fact(origin, load_restore_materials(origin, f"C{mapping['source_index']}").node)
            current = fixture.tool_fact(actual, actual.get(Node, mapping["node_id"]))
            assert original["request"] == current["request"] == request
            assert original["definitive"] and current["definitive"]
            if prepared:
                assert original["exit_code"] == current["exit_code"] == 0
                assert original["output"] == current["output"]
            facts.append(current)
    if prepared:
        assert prepared_counts == [0]
        assert report["services_manifest"]["selected"] == ["A"]
        service, = report["preparation"]["services"]
        assert service["name"] == "A" and service["status"] == "ready"
        assert service["startup"]["command"] == fixture.START_COMMAND
        assert service["startup"]["exit_code"] == 0
        assert "readiness" not in service and "final_readiness" not in service
        assert facts[0]["request"]["action"]["command"] == fixture.REQUEST_COMMAND
        assert json.loads(facts[0]["output"]) == {"status": 200, "body": fixture.BODY}
        assert "PALIN_SERVICE_A_STOPPED requests=1" in facts[1]["output"]
    else:
        assert report["stop_reason"] == "exit-code"
        mismatch = report["exit_mismatch"]
        assert (mismatch["source_index"], mismatch["source_exit_code"], mismatch["actual_exit_code"]) == (2, 0, 1)
        assert json.loads(facts[0]["output"])["error"] == "ConnectionRefusedError"
    evidence = {"cli_args": command, "cli_exit_code": code, "historical_tools": facts,
                "historical_request_executions": 1, "prepared_business_requests": prepared_counts,
                "execution_status": report["execution_status"]}
    write_json(output / "execution-check.json", evidence)
    return evidence


def main(preload, output, llm_config=None):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    summary = {"status": "failed", "output": str(output), "phase": "record"}
    write_json(output / "acceptance.json", summary)
    client = connect_docker(600)
    containers = {c.id: c.status for c in client.containers.list(all=True)}
    source = output / "source"
    try:
        recording = fixture.record_source(preload, source, client,
                                          decisions=commands(), files=fixture_files())
        assert recording["cleanup_confirmed"] and fixture.absent(client, recording["container_id"])
        assert len(recording["tools"]) == 3
        assert recording["tools"][0]["output"] == "PALIN_SERVICE_A_STARTED\nPALIN_SERVICE_A_READY requests=0\n"
        before = fixture.store_hashes(source / "record")
        write_json(source / "identity.json", {"store_hashes": before})
        write_json(source / "snapshot-check.json", snapshot_evidence(client, source))
        summary.update(source_run_id=recording["run_id"], phase="preprocess")
        write_json(output / "acceptance.json", summary, replace=True)
        preprocess = output / "preprocess"
        summary["preprocess_mode"] = preprocess_source(
            source, preprocess, recording["run_id"], llm_config,
        )
        manifest_path = preprocess / "services.json"
        manifest = verify_manifest(source, manifest_path, recording["run_id"])
        summary.update(manifest=manifest.model_dump(mode="json"),
                       manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                       replays=[], phase="replay")
        replays = []
        for name in ("with-services-1", "with-services-2", "without-services"):
            write_json(output / "acceptance.json", summary, replace=True)
            target = output / name
            evidence = replay_once(source, target,
                                   None if name == "without-services" else manifest_path,
                                   recording["run_id"])
            if name != "without-services":
                write_json(target / "snapshot-check.json", snapshot_evidence(client, source, target))
            assert fixture.store_hashes(source / "record") == before
            assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == summary["manifest_sha256"]
            summary["replays"].append({"output": str(target), **evidence})
            replays.append(target)
        summary["phase"] = "cleanup"
        cleanup = fixture.check_resources(source, replays, client)
        write_json(output / "cleanup.json", cleanup)
        assert {c.id: c.status for c in client.containers.list(all=True)} == containers
        summary.update(status="passed", phase="finished", source_unchanged=True,
                       cleanup_confirmed=True, unrelated_containers_unchanged=True)
    except BaseException as error:
        summary.update(error_type=type(error).__name__, error=str(error))
        raise
    finally:
        client.close()
        write_json(output / "acceptance.json", summary, replace=True)
    print(f"PASS: fresh C1-C3 source, A [1,3), two prepared Replays and missing-service control -> {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preload", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="new independent acceptance directory")
    parser.add_argument("--llm-config", type=Path,
                        help="opt into live model analysis; default: verified fixture evidence oracle")
    args = parser.parse_args()
    main(args.preload, args.output, args.llm_config)
