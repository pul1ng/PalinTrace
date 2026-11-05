"""Real Resume service acceptance for the existing 40-step A/B/C recording.

Run from the project root with python -m examples.resume_service_validation.
All inspection commands use the delivered executor; source materials stay read-only.
"""

import argparse
import hashlib
import json
import shlex
import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from examples import resume_restore_validation as restoration
from palinreplay.llm import OpenAIChatClient
from palinreplay.preprocess.manifest import SCHEMA_VERSION
from palinreplay.record.recorder import Recorder
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.resume import restore_resume_start, select_resume_start
from palinreplay.resume import session as session_module
from palinreplay.resume import validation as validation_module
from palinreplay.resume.materials import load_resume_materials
from palinreplay.resume.services import load_resume_service_input
from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.io import outside_store, write_json
from palinreplay.storage.rootfs import SCOPE
from palinreplay.storage.store import RecordStore

FORBIDDEN = ("model_requests", "historical_tool_reexecutions", "branch_node_commits",
             "native_initializations", "unapproved_commands")
_ACTION = ContextVar("resume_service_acceptance_action", default=None)
_GROUP = ContextVar("resume_service_acceptance_group", default=None)
PYTHON = "/opt/miniconda3/bin/python -B"


@contextmanager
def no_new_work(counts, actions):
    """Allow supplied startups and explicit inspections, counting actual exec entries."""
    original_exec = RestoredExecutor.exec
    original_prepare = session_module.prepare_services

    def execute(executor, command, **kwargs):
        action = _ACTION.get()
        if action is None or action["executor"] != id(executor):
            restoration.forbidden(counts, "historical_tool_reexecutions")()
        category = action["category"]
        if category == "startup" and command not in action["commands"]:
            restoration.forbidden(counts, "unapproved_commands")()
        entry = {"group": _GROUP.get(), "category": category,
                 "container_id": executor._container.id, "command": command,
                 "cwd": kwargs.get("cwd") or executor._workdir,
                 "environment_overrides": kwargs.get("env", {}),
                 "timeout_sec": kwargs.get("timeout_sec", executor._timeout)}
        if category == "startup":
            entry["service"] = action["commands"][command]
        else:
            entry.update({key: action[key] for key in ("label", "service", "attempt") if key in action})
            if category == "request":
                entry["request_passed"] = False
        actions.append(entry)
        counts[category + "_commands"] += 1
        started = time.monotonic()
        try:
            result = original_exec(executor, command, **kwargs)
        except BaseException as error:
            entry.update(error_type=type(error).__name__, error=str(error),
                         duration_sec=time.monotonic() - started)
            raise
        entry.update(exit_code=result.exit_code, output=result.text, duration_sec=result.duration_sec)
        return result

    def prepare(executor, config, journal):
        assert all(item.readiness_command is None for item in config.services)
        token = _ACTION.set({"category": "startup", "executor": id(executor),
                             "commands": {item.start_command: item.name for item in config.services}})
        try:
            return original_prepare(executor, config, journal)
        finally:
            _ACTION.reset(token)

    with ExitStack() as stack:
        stack.enter_context(patch.object(RestoredExecutor, "exec", execute))
        stack.enter_context(patch.object(session_module, "prepare_services", prepare))
        for owner, method, counter in (
            (OpenAIChatClient, "complete", "model_requests"),
            (RestoredExecutor, "execute", "historical_tool_reexecutions"),
            (RecordStore, "commit_node", "branch_node_commits"),
            (Recorder, "commit_step", "branch_node_commits"),
            (Recorder, "start", "branch_node_commits"),
            (Recorder, "start_from_source", "branch_node_commits"),
        ):
            stack.enter_context(patch.object(owner, method, restoration.forbidden(counts, counter)))
        for target, counter in (
            ("palinreplay.agents.mini.load_native", "native_initializations"),
            ("palinreplay.agents.mini.MiniSweAgent.__init__", "native_initializations"),
            ("palinreplay.replay.step.execute_step", "historical_tool_reexecutions"),
            ("palinreplay.replay.run.execute_step", "historical_tool_reexecutions"),
            ("openai.resources.chat.completions.Completions.create", "model_requests"),
            ("openai.resources.chat.completions.AsyncCompletions.create", "model_requests"),
        ):
            stack.enter_context(patch(target, restoration.forbidden(counts, counter)))
        yield


def run_command(executor, label, command, *, category="inspection", **metadata):
    token = _ACTION.set({"category": category, "executor": id(executor), "label": label, **metadata})
    try:
        return executor.exec(command)
    finally:
        _ACTION.reset(token)


def check(executor, label, command):
    result = run_command(executor, label, command)
    assert result.exit_code == 0, (label, result.exit_code, result.text)
    return result.text


def python(code):
    return PYTHON + " -c " + shlex.quote(code)


def check_commands(session):
    executor = session.executor
    assert check(executor, "pwd", "pwd").strip() == "/testbed"
    environment = json.loads(check(executor, "environment", python(
        "import json,os; print(json.dumps({key:os.environ.get(key) for key in ('PAGER','TZ')}))")))
    assert environment == {"PAGER": "cat", "TZ": "Etc/UTC"}
    data = json.loads(check(executor, "business_input", python(
        "import json; from pathlib import Path; "
        "data=json.loads(Path('/testbed/.palin-service-demo/input.json').read_text()); "
        "print(json.dumps({'data':data,'sum':sum(data)}))")))
    assert data == {"data": [1, 2, 3, 4, 5, 6], "sum": 21}
    note = json.loads(check(executor, "live_note", python(
        "import json; from pathlib import Path; p=Path('/testbed/.palin-service-demo/live-note'); "
        "print(json.dumps({'is_symlink':p.is_symlink(),'content':p.read_text().strip()}))")))
    assert note == {"is_symlink": True, "content": "shared version 3"}
    marker = "/testbed/.palin-resume-services-" + uuid4().hex
    check(executor, "temporary_write", "printf resume-service-marker > " + marker)
    assert check(executor, "temporary_read", "cat " + marker) == "resume-service-marker"
    check(executor, "temporary_delete", f"rm {marker} && test ! -e {marker}")
    return {"status": "passed", "cwd": "/testbed", "environment": environment,
            "business_input": data, "live_note": note, "temporary_file": marker,
            "temporary_file_deleted": True}


def request_services(executor, actions, order):
    attempts = dict.fromkeys(("A", "B", "C"), 0)
    for name in order:
        attempts[name] += 1
        result = run_command(
            executor, name + str(attempts[name]),
            f"{PYTHON} /opt/palin-replay-service-fixture/{name}/request.py",
            category="request", service=name, attempt=attempts[name],
        )
        response = json.loads(result.text)
        entry = actions[-1]
        entry.update(http_status=response.get("status"), body=response.get("body"))
        assert result.exit_code == 0, entry
        assert response.get("status") == 200, entry
        assert response.get("body") == f"PALIN_SERVICE_{name}_OK", entry
        entry["request_passed"] = True


def validate_requests(actions, group, order):
    requests = [action for action in actions
                if action["group"] == group and action["category"] == "request"]
    assert [action["service"] for action in requests] == list(order), requests
    for name in ("A", "B", "C"):
        selected = [action for action in requests if action["service"] == name]
        assert [action["attempt"] for action in selected] == list(range(1, order.count(name) + 1))
    assert all(action["exit_code"] == 0 and action["http_status"] == 200
               and action["body"] == f"PALIN_SERVICE_{action['service']}_OK"
               and action["duration_sec"] >= 0 and action["request_passed"] for action in requests)
    return {name: sum(action["service"] == name for action in requests) for name in ("A", "B", "C")}


def check_closed_ports(executor, names):
    ports = {"A": 18765, "B": 18766, "C": 18767}
    selected = {name: ports[name] for name in names}
    code = (
        "import json,socket\n"
        f"ports={selected!r}\n"
        "for name,port in ports.items():\n"
        "    try:\n"
        "        connection=socket.create_connection(('127.0.0.1',port),timeout=1)\n"
        "    except ConnectionRefusedError:\n"
        "        continue\n"
        "    else:\n"
        "        connection.close()\n"
        "        raise AssertionError(name+' port is open')\n"
        "print(json.dumps({'closed':list(ports)}))\n"
    )
    result = json.loads(check(executor, "unselected_ports_closed", python(code)))
    assert result == {"closed": list(names)}
    return result


def main(source, services_config, output):
    source, output = Path(source).resolve(), Path(output).absolute()
    recording = source.parent if source.name == "record" else source
    outside_store(recording, output)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    counts = dict.fromkeys((*FORBIDDEN, "startup_commands", "inspection_commands", "request_commands"), 0)
    summary = {"status": "failed", "source": str(recording), "counts": counts,
               "actions": [], "r1_calls": [], "sessions": {}}
    write_json(output / "acceptance.json", summary)
    before = restoration.file_inventory(recording)
    write_json(output / "source-files-before.json", before)
    operations = []
    original_command, original_r1 = BtrfsPool.command, validation_module.validate_r1
    client = None
    started = time.monotonic()

    def command(pool, op, **values):
        entry = {"op": op, "pool": pool.name, "arguments": values}
        operations.append(entry)
        try:
            entry["result"] = original_command(pool, op, **values)
            return entry["result"]
        except BaseException as error:
            entry["error"] = str(error)
            raise

    def r1(materials, executor):
        entry = {"group": _GROUP.get(), "container_id": executor._container.id,
                 "commands_before_r1": len(summary["actions"])}
        summary["r1_calls"].append(entry)
        result = original_r1(materials, executor)
        entry.update(result=result, commands_after_r1=len(summary["actions"]))
        return result

    try:
        service_input = load_resume_service_input(services_config)
        assert service_input.manifest.schema_version == SCHEMA_VERSION
        summary["manifest"] = {"path": str(Path(services_config).resolve()),
                               "sha256": hashlib.sha256(Path(services_config).read_bytes()).hexdigest(),
                               "source": service_input.manifest.source.model_dump(mode="json")}
        with no_new_work(counts, summary["actions"]), patch.object(BtrfsPool, "command", command), \
                patch.object(validation_module, "validate_r1", r1):
            start = select_resume_start(source, "C20", model="M", max_steps=2)
            assert (start.node.node_id, start.node.run_id, start.node.state_id) == (
                restoration.EXPECTED_NODE_ID, restoration.EXPECTED_RUN_ID, restoration.EXPECTED_STATE_ID)
            materials = load_resume_materials(start)
            nodes_before = restoration.node_inventory(start.source_store)
            write_json(output / "source-nodes-before.json", nodes_before)
            client = connect_docker(600)
            summary["pool_before"] = restoration.pool_audit(client, materials.snapshot)
            for index, node_id, selected, order, closed in (
                (20, restoration.EXPECTED_NODE_ID, ["A", "B"], ["A", "B"] * 3, ["C"]),
                (35, "fd94fb57b2944f98affd89b2a931d308", ["C"], ["C"] * 3, ["A", "B"]),
            ):
                group = f"C{index}"
                start = select_resume_start(source, group, model="M", max_steps=2)
                assert start.node.node_id == node_id
                group_materials = load_resume_materials(start)
                snapshot_before = restoration.pool_audit(client, group_materials.snapshot)
                destination = output / group
                entry = {"status": "in_progress", "report": str(destination / "report.json"),
                         "restore_report": str(destination / "restore-report.json"),
                         "preparation_report": str(destination / "preparation.json")}
                summary["sessions"][group] = entry
                token = _GROUP.set(group)
                try:
                    print(f"Restoring {group}, preparing {selected}, and making requests", flush=True)
                    with restore_resume_start(start, destination, client,
                                              services_config=services_config) as session:
                        assert session.report["r1"]["status"] == "passed"
                        assert session.report["r1"]["scope"] == SCOPE
                        assert session.report["preparation"]["status"] == "ready"
                        entry["prefix"] = restoration.compare_prefix(session)
                        if index == 20:
                            entry["commands"] = check_commands(session)
                        request_services(session.executor, summary["actions"], order)
                        entry["closed_ports"] = check_closed_ports(session.executor, closed)
                except BaseException as error:
                    entry.update(status="failed", error_type=type(error).__name__, error=str(error))
                    raise
                finally:
                    _GROUP.reset(token)
                report = restoration.checked_report(client, destination)
                assert report["status"] == "passed"
                assert [item["name"] for item in report["preparation"]["services"]] == selected
                actions = [action for action in summary["actions"] if action["group"] == group]
                startups = [action for action in actions if action["category"] == "startup"]
                assert [action["service"] for action in startups] == selected
                container = session.restored.container.id
                assert all(action["container_id"] == container for action in actions)
                r1_call, = [call for call in summary["r1_calls"] if call["group"] == group]
                assert r1_call["container_id"] == container
                assert r1_call["commands_before_r1"] == r1_call["commands_after_r1"]
                assert r1_call["commands_after_r1"] == summary["actions"].index(actions[0])
                snapshot_after = restoration.pool_audit(client, group_materials.snapshot)
                assert snapshot_before["source"] == snapshot_after["source"]
                assert snapshot_before["workspaces"] == snapshot_after["workspaces"]
                entry.update(
                    status="passed", container_id=container,
                    startup_counts={name: sum(action["service"] == name for action in startups)
                                    for name in ("A", "B", "C")},
                    request_counts=validate_requests(summary["actions"], group, order),
                    same_container=True, r1_before_startup=True, cleanup_confirmed=True,
                    snapshot_before=snapshot_before, snapshot_after=snapshot_after,
                )
            assert len(summary["r1_calls"]) == 2
            assert summary["sessions"]["C20"]["container_id"] != summary["sessions"]["C35"]["container_id"]
            summary["pool_after"] = restoration.pool_audit(client, materials.snapshot)
            assert summary["pool_before"]["source"] == summary["pool_after"]["source"]
            assert summary["pool_before"]["workspaces"] == summary["pool_after"]["workspaces"]
            nodes_after = restoration.node_inventory(start.source_store)
            write_json(output / "source-nodes-after.json", nodes_after)
            assert nodes_before == nodes_after and len(nodes_before) == 41
            assert all(counts[name] == 0 for name in FORBIDDEN), counts
            summary.update(status="passed", source_nodes_unchanged=True,
                           source_snapshot_unchanged=True, temporary_resources_released=True)
    except BaseException as error:
        summary.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        if client is not None:
            client.close()
        after = restoration.file_inventory(recording)
        write_json(output / "source-files-after.json", after)
        summary["source_files_unchanged"] = before == after
        if before != after:
            summary["status"] = "failed"
        summary["duration_sec"] = time.monotonic() - started
        write_json(output / "pool-operations.json", operations)
        write_json(output / "acceptance.json", summary, replace=True)
    assert summary["status"] == "passed", summary
    print(json.dumps({"status": summary["status"], "counts": counts, "output": str(output)}))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--services-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="new directory outside recording")
    arguments = parser.parse_args()
    main(arguments.source, arguments.services_config, arguments.output)
