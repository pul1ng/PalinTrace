"""Real missing-service Replay acceptance with fixed decisions and no model service."""

import json
import shlex
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from r1_restore_validation import absent, store_hashes
from replay_validation import fixed_response

from palinreplay.agents import AgentTask
from palinreplay.agents.mini import MiniSweAgent, load_native
from palinreplay.core.models import Node
from palinreplay.preload.base import open_preload
from palinreplay.record.calls import read_call
from palinreplay.record.restore import load_restore_materials
from palinreplay.record.state import BtrfsStateBackend
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.replay.plan import pin_replay
from palinreplay.replay.session import run_replay
from palinreplay.sandbox.btrfs import BtrfsWorkspace
from palinreplay.storage.btrfs.models import BtrfsRuntime
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.io import write_json
from palinreplay.storage.rootfs import SCOPE
from palinreplay.storage.store import RecordStore

DIRECTORY = "/opt/palin-replay-service-fixture"
PYTHON = "/opt/miniconda3/bin/python"
ADDRESS = "http://127.0.0.1:18765/"
BODY = "PALIN_SERVICE_OK"
CONFIG = {"host": "127.0.0.1", "port": 18765, "body": BODY, "timeout_sec": 2}

SERVER = '''import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

config = json.loads(Path(__file__).with_name("config.json").read_text())

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = config["body"].encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass

HTTPServer((config["host"], config["port"]), Handler).serve_forever()
'''

REQUEST = '''import http.client
import json
import sys
from pathlib import Path

config = json.loads(Path(__file__).with_name("config.json").read_text())
connection = http.client.HTTPConnection(config["host"], config["port"],
                                        timeout=config["timeout_sec"])
try:
    connection.request("GET", "/")
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    print(json.dumps({"status": response.status, "body": body}, sort_keys=True))
    code = 0 if response.status == 200 and body == config["body"] else 2
except (OSError, http.client.HTTPException) as error:
    print(json.dumps({"error": type(error).__name__, "errno": getattr(error, "errno", None),
                      "message": str(error)}, sort_keys=True))
    code = 1
finally:
    connection.close()
sys.exit(code)
'''


LIFECYCLE = '''import http.client
import json
import os
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

def ready():
    connection = http.client.HTTPConnection(config["host"], config["port"], timeout=0.5)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        return response.status == 200 and response.read().decode() == config["body"]
    except OSError:
        return False
    finally:
        connection.close()

if sys.argv[1] == "start":
    if not port_closed():
        raise RuntimeError("fixture port already in use")
    child = subprocess.Popen([sys.executable, "-B", str(root / "server.py")],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while True:
            if child.poll() is not None:
                raise RuntimeError("service exited before readiness")
            if ready() and child.poll() is None:
                identity.write_text(json.dumps({"pid": child.pid,
                                                "start": process(child.pid)["start"]}))
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("service readiness deadline exceeded")
            time.sleep(0.05)
    except BaseException:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
        raise
    print("PALIN_SERVICE_READY")
elif sys.argv[1] == "stop":
    saved = json.loads(identity.read_text())
    current = process(saved["pid"])
    if current is None or current["start"] != saved["start"] or current["state"] == "Z":
        raise RuntimeError("this fixture service is not running")
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
    print("PALIN_SERVICE_STOPPED")
else:
    raise ValueError("expected start or stop")
'''


def python_command(code):
    return f"{PYTHON} -B -c " + shlex.quote(code)


def fixture_files():
    return {"server.py": SERVER, "request.py": REQUEST, "lifecycle.py": LIFECYCLE,
            "config.json": json.dumps(CONFIG, sort_keys=True) + "\n"}


def create_files_command():
    return python_command(
        "from pathlib import Path; "
        f"directory = Path({DIRECTORY!r}); directory.mkdir(); "
        f"[(directory / name).write_text(body) for name, body in {fixture_files()!r}.items()]"
    )


REQUEST_COMMAND = f"{PYTHON} -B {DIRECTORY}/request.py"
START_COMMAND = f"{PYTHON} -B {DIRECTORY}/lifecycle.py start"
STOP_COMMAND = f"{PYTHON} -B {DIRECTORY}/lifecycle.py stop"


def commands():
    confirm = python_command(
        "from pathlib import Path; "
        f"root = Path({DIRECTORY!r}); "
        f"assert all((root/name).is_file() for name in {list(fixture_files())!r}); "
        "assert (root/'service.json').is_file()"
    )
    return [
        create_files_command() + " && " + START_COMMAND,
        confirm + " && " + REQUEST_COMMAND,
        REQUEST_COMMAND,
        STOP_COMMAND + " >/dev/null && printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\nservice fixture complete\\n'",
    ]


@contextmanager
def no_model_calls():
    """Reject the native model transport, while permitting fixed completion replies."""
    native = load_native()
    from minisweagent.models import litellm_model

    with patch.object(native.LitellmModel, "_query",
                      side_effect=AssertionError("external model calls are forbidden")) as transport:
        with patch.object(litellm_model.litellm.cost_calculator,
                          "completion_cost", return_value=0):
            yield transport
        assert transport.call_count == 0


def tool_fact(store, node):
    if len(node.tool_execution_ids) != 1:
        raise AssertionError("fixture requires one tool per committed step")
    trace = read_call(store, node.tool_execution_ids[0])
    if trace.phase != "accepted":
        raise AssertionError("tool result was not accepted")
    result = next(event for event in trace.events if event.kind == "result")
    details = store.read_json(result.details)
    return {
        "node_id": node.node_id, "index": node.index,
        "execution_id": trace.execution.execution_id,
        "request": store.read_json(trace.execution.request),
        "exit_code": details["backend"]["decoded"]["returncode"],
        "output": store.read_bytes(result.payload).decode("utf-8"),
        "accepted": store.read_json(trace.events[-1].payload),
    }


def record_source(preload, output, client):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    report = {"status": "failed", "fixed_response_supplies": 0,
              "actual_model_calls": 0, "actual_model_cost": 0, "cleanup_confirmed": False}
    write_json(output / "report.json", report)
    base = open_preload(preload)
    launch = base.metadata.launch.model_copy(update={"network_mode": "none"})
    try:
        with BtrfsPool(client, base.metadata.snapshot.location) as pool:
            workspace = BtrfsWorkspace(pool, base.metadata.snapshot, uuid4().hex)
            report["resources"] = {
                "container_name": workspace.container_name, "volume_name": workspace.volume_name,
                "workspace": workspace.name, "pool_container_name": pool.name,
                "pool_volume": pool.location.volume,
            }
            try:
                workspace.start(launch, base.metadata.bootstrap_image_id,
                                environment=launch.environment, labels={})
                report["container_id"] = workspace.container.id
                runtime = BtrfsRuntime(
                    source_container_id=workspace.container.id,
                    source_image_id=base.metadata.source_image_id,
                    bootstrap_image_id=base.metadata.bootstrap_image_id, launch=launch,
                    c0_oracle_sha256=base.metadata.oracle_sha256,
                )
                restored = SimpleNamespace(pool=pool, workspace=workspace,
                                           container=workspace.container, close=workspace.close)
                sandbox = RestoredExecutor(restored, runtime)
                sandbox.inspect = lambda: SimpleNamespace(
                    actual_image_id=runtime.bootstrap_image_id, workdir=launch.workdir,
                    environment=launch.environment, shell=launch.shell,
                )
                backend = BtrfsStateBackend(sandbox, public_env_keys=launch.environment,
                                            runtime=runtime)
                replies = iter(fixed_response(cmd, i) for i, cmd in enumerate(commands(), 1))

                def completion(**request):
                    report["fixed_response_supplies"] += 1
                    return next(replies)

                with RecordStore.create(output / "record") as store:
                    agent = MiniSweAgent(
                        sandbox=sandbox, store=store, capture_state=backend.capture,
                        model_config={"model_name": "openai/replay-fixture",
                                      "cost_tracking": "ignore_errors",
                                      "model_kwargs": {"num_retries": 0}},
                        agent_config={"system_template": "Fixed HTTP service fixture",
                                      "instance_template": "{{task}}", "step_limit": 5,
                                      "cost_limit": 0},
                        completion=completion, retry_attempts=1,
                    )
                    with no_model_calls():
                        agent.run(AgentTask("astropy-service-fixture", "Execute the fixed decisions."))
                    nodes = sorted(store.list(Node), key=lambda node: node.index)
                    assert [node.index for node in nodes] == list(range(5))
                    assert report["fixed_response_supplies"] == 4
                    facts = [tool_fact(store, node) for node in nodes[1:]]
                    assert all(fact["exit_code"] == 0 for fact in facts), facts
                    assert json.loads(facts[2]["output"]) == {"status": 200, "body": BODY}
                    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in facts[3]["output"]
                    c2 = load_restore_materials(store, nodes[2].node_id)
                    check = pool.container.exec_run(["python3", "-B", "-c",
                        "from pathlib import Path; "
                        f"root = Path('/pool/{c2.snapshot.name}{DIRECTORY}'); "
                        f"assert all((root/name).read_text() == body "
                        f"for name, body in {fixture_files()!r}.items())"])
                    assert check.exit_code == 0, check.output
                    report.update(status="recorded", run_id=nodes[0].run_id,
                                  node_ids=[node.node_id for node in nodes],
                                  tools=facts, c2_fixture_files_verified=True)
            finally:
                workspace.close()
                report["workspace_cleanup_confirmed"] = True
        report["pool_cleanup_confirmed"] = True
        report["cleanup_confirmed"] = absent(client, report["container_id"])
        assert report["cleanup_confirmed"]
        return report
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        write_json(output / "report.json", report, replace=True)


def volume_absent(client, name):
    try:
        client.volumes.get(name)
    except Exception as error:
        if getattr(error, "status_code", None) == 404:
            return True
        raise
    return False


def source_identity(source, client):
    """Persist a fixed C2-C4 selection only after original-instance removal."""
    source = Path(source).absolute()
    recording = json.loads((source / "report.json").read_text())
    cleanup = {"container_removed": absent(client, recording["container_id"]),
               "record_cleanup_confirmed": recording["cleanup_confirmed"]}
    if not all(cleanup.values()):
        raise AssertionError("source container cleanup is unconfirmed; Replay is forbidden")
    with RecordStore.open(source / "record") as store:
        plan = pin_replay(store, "C2", "C4", run_id=recording["run_id"])
        materials = [load_restore_materials(store, node.node_id) for node in plan.nodes]
        c2 = materials[0]
        assert c2.runtime.source_container_id == recording["container_id"]
        assert c2.runtime.launch.network_mode == "none"
        resources = recording["resources"]
        cleanup.update(
            task_container_removed=absent(client, resources["container_name"]),
            root_volume_removed=volume_absent(client, resources["volume_name"]),
            pool_helper_removed=absent(client, resources["pool_container_name"]),
        )
        with BtrfsPool(client, c2.snapshot.location) as pool:
            snapshots = []
            for item in materials:
                pool.verify(item.snapshot)
                snapshots.append(item.snapshot.model_dump(mode="json"))
            check = pool.container.exec_run(["python3", "-B", "-c",
                "from pathlib import Path; "
                f"assert not Path('/pool/{resources['workspace']}').exists(); "
                f"root = Path('/pool/{c2.snapshot.name}{DIRECTORY}'); "
                f"assert all((root/name).read_text() == body "
                f"for name, body in {fixture_files()!r}.items())"])
            assert check.exit_code == 0, check.output
            cleanup["workspace_removed"] = True
        cleanup["pool_helper_removed"] = absent(client, resources["pool_container_name"])
        client.volumes.get(c2.snapshot.location.volume)
        cleanup["persistent_pool_retained"] = True
        assert all(cleanup.values()), cleanup
        identity = {"source_store": str((source / "record").resolve()),
                    "selection": plan.provenance(), "snapshots": snapshots,
                    "store_hashes": store_hashes(source / "record"),
                    "cleanup": cleanup, "snapshot_files_readable": True}
    write_json(source / "identity.json", identity)
    write_json(source / "cleanup.json", cleanup)
    return identity


def replay_once(source, output, client):
    source, output = Path(source), Path(output)
    identity = json.loads((source / "identity.json").read_text())
    recording = json.loads((source / "report.json").read_text())
    assert absent(client, recording["container_id"]), "source container must be absent before Replay"
    assert all(identity["cleanup"].values()), "source cleanup was not confirmed"
    assert store_hashes(source / "record") == identity["store_hashes"]
    executed = []
    execute = RestoredExecutor.execute

    def observe(executor, request):
        executed.append(request)
        return execute(executor, request)

    with no_model_calls(), patch.object(RestoredExecutor, "execute", observe):
        report = run_replay(source / "record", "C2", "C4",
                            run_id=identity["selection"]["run"]["run_id"],
                            output=output, client=client)
    write_json(output / "execution-check.json", {
        "source_selection_sha256": identity["selection"]["selection_sha256"],
        "executed_requests": executed, "actual_model_calls": 0,
    })
    assert report["source"]["selection_sha256"] == identity["selection"]["selection_sha256"]
    assert report["mappings"][0]["source_index"] == 2
    assert len(executed) == 1 and executed[0]["action"]["command"] == REQUEST_COMMAND
    assert report["statistics"]["actual_tool_executions"] == 1
    assert report["statistics"]["actual_model_calls"] == 0
    assert report["cleanup_confirmed"]
    return report


def check_r1(source, replay, client):
    source, replay = Path(source), Path(replay)
    result = {"status": "failed"}
    try:
        with RecordStore.open(source / "record") as origin, \
                RecordStore.open(replay / "record") as current:
            expected = load_restore_materials(origin, "C2")
            actual = load_restore_materials(current, "C0")
            expected_context = origin.read_json(expected.node.context)
            actual_context = current.read_json(actual.node.context)
            historical_position = origin.read_json(expected.node.input_position)
            config = current.read_json(actual.run.config)
            inherited = {key: value for key, value in actual_context.items() if key != "replay"}
            checks = {
                "inherited_context": inherited == expected_context,
                "messages": actual_context["messages"] == expected_context["messages"],
                "inherited_config": {key: value for key, value in config.items() if key != "replay"}
                                    == origin.read_json(expected.run.config),
                "counters": actual_context["counters"] == expected_context["counters"],
                "historical_input_position": actual_context["replay"] == {
                    "historical_input_position": historical_position,
                    "historical_answer_supplies": 0, "actual_model_calls": 0,
                    "actual_model_cost": 0,
                },
                "local_input_position": current.read_json(actual.node.input_position) == {
                    "next_message": len(expected_context["messages"]), "adoptions": [],
                },
                "independent_run": actual.run.run_id != expected.run.run_id
                                   and actual.run.source_node_id is None,
                "source_identity": config["replay"]["source"]
                                   == json.loads((source / "identity.json").read_text())["selection"],
                "starts_before_C3": len(actual_context["messages"])
                                    < len(origin.read_json(load_restore_materials(origin, "C3")
                                                           .node.context)["messages"]),
            }
            result["context_checks"] = checks
            assert all(checks.values()), checks
            assert expected.snapshot.location == actual.snapshot.location
            with BtrfsPool(client, expected.snapshot.location) as pool:
                pool.verify(expected.snapshot)
                pool.verify(actual.snapshot)
                compared = pool.command("verify", source=expected.snapshot.name,
                                        workspace=actual.snapshot.name, scope=SCOPE,
                                        oracle_sha256=expected.runtime.c0_oracle_sha256)
                roots = pool.container.exec_run(["python3", "-B", "-c",
                    "import json, os, stat; "
                    f"paths = {[f'/pool/{s.name}' for s in (expected.snapshot, actual.snapshot)]!r}; "
                    "print(json.dumps([{'type': stat.S_IFMT(os.stat(p).st_mode), "
                    "'mode': stat.S_IMODE(os.stat(p).st_mode), 'uid': os.stat(p).st_uid, "
                    "'gid': os.stat(p).st_gid} for p in paths]))"])
                assert roots.exit_code == 0, roots.output
                source_root, actual_root = json.loads(roots.output)
                assert source_root == actual_root
                result.update(files=compared, root_attributes={
                    "source": source_root, "actual": actual_root, "equal": True,
                })
            result.update(status="passed", source_node_id=expected.node.node_id,
                          node_id=actual.node.node_id,
                          source_snapshot=expected.snapshot.model_dump(mode="json"),
                          actual_snapshot=actual.snapshot.model_dump(mode="json"))
            return result
    except Exception as error:
        result.update(error_type=type(error).__name__, error=str(error))
        raise
    finally:
        write_json(replay / "r1-check.json", result)
