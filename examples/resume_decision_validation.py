"""Opt-in C20 decision acceptance: two controlled inputs and one live model request.

Run with python -m examples.resume_decision_validation. Restoration, A/B probes,
read-only inventories and cleanup run outside the guarded decision interval.
"""

import argparse
import hashlib
import json
import shlex
import time
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx

from examples import resume_restore_validation as restoration
from palinreplay.agents.mini import MiniSweAgent, load_native
from palinreplay.core.models import Node
from palinreplay.record.recorder import Recorder
from palinreplay.record.state import BtrfsStateBackend
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.resume import decide_resume_start, restore_resume_start, select_resume_start
from palinreplay.resume.materials import load_resume_materials
from palinreplay.resume.services import load_resume_service_input, resolve_resume_services
from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.io import outside_store, write_json
from palinreplay.storage.store import RecordStore

INSTRUCTION = "下一步先检查 B 的日志"
ZERO_COUNTS = ("tool_executions", "task_file_updates", "file_snapshots", "complete_nodes",
               "recording_operations", "agent_runs")


def semantic_messages(messages):
    """Compare conversation data across the SDK's omission of null/metadata fields."""
    result = []
    for message in messages:
        item = {key: message[key] for key in ("role", "content", "tool_call_id")
                if message.get(key) is not None}
        if message.get("tool_calls"):
            item["tool_calls"] = [{
                "id": call["id"], "type": call["type"],
                "function": {key: call["function"][key] for key in ("name", "arguments")},
            } for call in message["tool_calls"]]
        result.append(item)
    return result


@contextmanager
def decision_boundary(session, counts, *, controlled=None):
    """Permit one model query and reject every environment/recording operation."""
    native = load_native()
    original_completion, original_send = native.litellm.completion, httpx.Client.send

    def forbidden(name):
        def fail(*args, **kwargs):
            counts[name] += 1
            raise AssertionError(f"Decision attempted {name}")
        return fail

    def complete(**request):
        counts["model_requests"] += 1
        assert counts["model_requests"] == 1
        evidence = json.loads(session.report_path.read_text())["decision"]
        public = {key: value for key, value in request.items() if key not in ("api_key", "api_base")}
        assert evidence["request"] == public and evidence["status"] == "prepared"
        assert request["api_key"] not in json.dumps(session.report)
        assert public["num_retries"] == public["max_retries"] == 0
        expected = deepcopy(session.prefix.messages)
        if evidence["instruction"] is not None:
            expected.append({"role": "user", "content": evidence["instruction"]})
        assert semantic_messages(public["messages"]) == semantic_messages(expected)
        assert public["model"] == session.report["decision"]["model"]
        return controlled if controlled is not None else original_completion(**request)

    def send(client, request, **kwargs):
        if request.method == "POST" and request.url.path.endswith("/chat/completions"):
            counts["http_requests"] += 1
            body = json.loads(request.content)
            public = session.report["decision"]["request"]
            assert semantic_messages(body["messages"]) == semantic_messages(public["messages"])
            assert body["tools"] == public["tools"]
            assert body["model"] == public["model"].removeprefix("openai/")
        return original_send(client, request, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(native.litellm, "completion", complete))
        stack.enter_context(patch.object(httpx.Client, "send", send))
        for owner, method, counter in (
            (RestoredExecutor, "exec", "tool_executions"),
            (RestoredExecutor, "execute", "tool_executions"),
            (native.BorrowedEnvironment, "execute", "tool_executions"),
            (type(session.restored.container), "exec_run", "tool_executions"),
            (BtrfsPool, "command", "file_snapshots"),
            (BtrfsStateBackend, "capture", "file_snapshots"),
            (RecordStore, "commit_node", "complete_nodes"),
            (Recorder, "commit_step", "complete_nodes"),
            (Recorder, "begin_step", "recording_operations"),
            (Recorder, "start", "recording_operations"),
            (Recorder, "start_from_source", "recording_operations"),
            (MiniSweAgent, "run", "agent_runs"),
        ):
            stack.enter_context(patch.object(owner, method, forbidden(counter)))
        yield


def inspect(executor, actions, label, command):
    result = executor.exec(command)
    entry = {"label": label, "command": command, "container_id": executor._container.id,
             "exit_code": result.exit_code, "output": result.text}
    actions.append(entry)
    assert result.exit_code == 0, entry
    return result.text


def task_inventory(executor, actions, label):
    code = """import hashlib,json,os,stat
from pathlib import Path
root=Path('/testbed')
h=hashlib.sha256()
count=0
for directory, dirs, files in os.walk(root, followlinks=False):
    dirs.sort()
    for name in sorted(dirs+files):
        p=Path(directory)/name
        mode=p.lstat().st_mode
        if stat.S_ISLNK(mode):
            value=['link',os.readlink(p)]
        elif stat.S_ISREG(mode):
            value=['file',hashlib.sha256(p.read_bytes()).hexdigest()]
        else:
            value=['other',stat.S_IFMT(mode)]
        h.update(json.dumps([str(p.relative_to(root)),stat.S_IMODE(mode),value]).encode())
        count+=1
print(json.dumps({'entries':count,'sha256':h.hexdigest()}))
"""
    command = "/opt/miniconda3/bin/python -B -c " + shlex.quote(code)
    return json.loads(inspect(executor, actions, label, command))


def controlled_response(marker):
    from litellm import ModelResponse

    commands = [f"printf 'pending-stage4-only' > {shlex.quote(marker)}",
                "/opt/miniconda3/bin/python -B /opt/palin-replay-service-fixture/B/request.py"]
    return ModelResponse(
        id="resume-controlled-" + uuid4().hex, model="controlled-M", created=1,
        choices=[{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "A new decision, with two pending commands.",
            "tool_calls": [{"id": f"branch-tool-{index}", "type": "function", "function": {
                "name": "bash", "arguments": json.dumps({"command": command}),
            }} for index, command in enumerate(commands, 1)],
        }}], usage={"prompt_tokens": 420, "completion_tokens": 43, "total_tokens": 463},
    )


def run_case(start, services_config, destination, client, *, instruction, live, llm_config, entry):
    marker = "/testbed/.palin-resume-decision-" + uuid4().hex
    actions = entry["inspection_actions"] = []
    counts = entry["counts"] = dict.fromkeys((*ZERO_COUNTS, "model_requests", "http_requests"), 0)
    response = None if live else controlled_response(marker)
    try:
        with restore_resume_start(start, destination, client,
                                  services_config=services_config) as session:
            assert session.report["restore_status"] == session.report["r1"]["status"] == "passed"
            preparation = session.report["preparation"]
            assert preparation["status"] == "ready"
            assert [service["name"] for service in preparation["services"]] == ["A", "B"]
            assert all(service["startup"]["exit_code"] == 0 for service in preparation["services"])
            entry["prefix"] = restoration.compare_prefix(session, model=start.model)
            saved = deepcopy(session.prefix.context)
            for name in ("A", "B"):
                command = f"/opt/miniconda3/bin/python -B /opt/palin-replay-service-fixture/{name}/request.py"
                body = json.loads(inspect(session.executor, actions, f"{name}_ready", command))
                assert body["status"] == 200 and body["body"] == f"PALIN_SERVICE_{name}_OK"
            before = task_inventory(session.executor, actions, "task_files_before")
            inspect(session.executor, actions, "marker_absent_before", "test ! -e " + marker)
            with decision_boundary(session, counts, controlled=response):
                decision = decide_resume_start(session, instruction=instruction, llm_config=llm_config)
            after = task_inventory(session.executor, actions, "task_files_after")
            if before != after:
                counts["task_file_updates"] += 1
            assert before == after
            inspect(session.executor, actions, "marker_absent_after", "test ! -e " + marker)
            assert session.prefix.context == saved
            assert entry["prefix"] == restoration.compare_prefix(session, model=start.model)
            assert decision.messages[:42] == saved["messages"]
            assert len(decision.messages) == 42 + (instruction is not None)
            if instruction is not None:
                assert decision.messages[42] == {"role": "user", "content": instruction}
            if response is not None:
                assert decision.raw_response == response.model_dump(mode="json")
                assert [action["tool_call_id"] for action in decision.actions] == [
                    "branch-tool-1", "branch-tool-2"]
                calls = response.choices[0].message.tool_calls
                assert [action["arguments"] for action in decision.actions] == [
                    json.loads(call.function.arguments) for call in calls]
                assert marker in decision.actions[0]["command"]
                with RecordStore.open(start.source_store, read_only=True) as store:
                    next_id, = store.find_node_ids(index=21, run_id=start.node.run_id)
                    historical = store.read_json(store.get(Node, next_id).context)["messages"][42]
                assert decision.assistant_message["tool_calls"] != historical["tool_calls"]
            accounting = decision.accounting
            assert accounting["inherited_counters"]["n_calls"] == 20
            assert accounting["model_requests"] == accounting["decisions"] == counts["model_requests"] == 1
            assert accounting["max_steps"] == 2 and accounting["remaining_steps"] == 1
            assert counts["http_requests"] == int(live)
            assert all(counts[name] == 0 for name in ZERO_COUNTS), counts
            assert decision.actions
            entry.update(status="passed", container_id=session.restored.container.id,
                         input_message_count=len(decision.messages), model=session.report["decision"]["model"],
                         assistant_message=decision.assistant_message, actions=decision.actions,
                         accounting=accounting, task_files_before=before, task_files_after=after,
                         marker=marker, marker_absent=True, source_prefix_unchanged=True)
    except BaseException as error:
        entry.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        if (destination / "report.json").is_file():
            report = json.loads((destination / "report.json").read_text())
            entry["cleanup_confirmed"] = report["cleanup_confirmed"]
    report = restoration.checked_report(client, destination)
    assert report["status"] == "passed"
    entry["report"] = str(Path(destination.name) / "report.json")


def main(source, services_config, output, *, model, llm_config):
    source, services_config = Path(source).resolve(), Path(services_config).resolve()
    output = Path(output).absolute()
    recording = source.parent if source.name == "record" else source
    outside_store(recording, output)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    before = restoration.file_inventory(recording)
    manifest_digest = hashlib.sha256(services_config.read_bytes()).hexdigest()
    summary = {"status": "in_progress", "source": str(source), "model": model,
               "manifest": str(services_config), "cases": {}, "stage4_complete": False}
    write_json(output / "acceptance.json", summary)
    write_json(output / "source-files-before.json", before)
    client = None
    started = time.monotonic()
    try:
        start = select_resume_start(source, "C20", model=model, max_steps=2,
                                    run_id=restoration.EXPECTED_RUN_ID)
        assert (start.node.node_id, start.node.state_id) == (
            restoration.EXPECTED_NODE_ID, restoration.EXPECTED_STATE_ID)
        materials = load_resume_materials(start)
        selected = resolve_resume_services(start, load_resume_service_input(services_config))
        assert [service.name for service in selected.services] == ["A", "B"]
        nodes_before = restoration.node_inventory(start.source_store)
        write_json(output / "source-nodes-before.json", nodes_before)
        client = connect_docker(600)
        summary["pool_before"] = restoration.pool_audit(client, materials.snapshot)
        for name, instruction, live in (("default", None, False), ("guided", INSTRUCTION, False),
                                        ("real", INSTRUCTION, True)):
            entry = summary["cases"][name] = {"status": "in_progress", "live": live}
            print(f"C20 {name}: restoring, preparing A/B, then deciding once", flush=True)
            run_case(start, services_config, output / name, client,
                     instruction=instruction, live=live, llm_config=llm_config, entry=entry)
            write_json(output / "acceptance.json", summary, replace=True)
        summary["pool_after"] = restoration.pool_audit(client, materials.snapshot)
        assert summary["pool_before"]["source"] == summary["pool_after"]["source"]
        assert summary["pool_before"]["workspaces"] == summary["pool_after"]["workspaces"]
        nodes_after = restoration.node_inventory(start.source_store)
        write_json(output / "source-nodes-after.json", nodes_after)
        assert nodes_before == nodes_after and len(nodes_before) == 41
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
        summary["manifest_unchanged"] = manifest_digest == hashlib.sha256(services_config.read_bytes()).hexdigest()
        if not summary["source_files_unchanged"] or not summary["manifest_unchanged"]:
            summary["status"] = "failed"
        summary["stage4_complete"] = summary["status"] == "passed" and all(
            case["status"] == "passed" and case.get("cleanup_confirmed")
            for case in summary["cases"].values()) and len(summary["cases"]) == 3
        summary["duration_sec"] = time.monotonic() - started
        write_json(output / "acceptance.json", summary, replace=True)
    assert summary["stage4_complete"], summary
    print(json.dumps({"status": "passed", "stage4_complete": True, "output": str(output)}))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--services-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", required=True, help="selected LiteLLM model, e.g. openai/<gateway model>")
    parser.add_argument("--llm-config", required=True, type=Path)
    arguments = parser.parse_args()
    main(arguments.source, arguments.services_config, arguments.output,
         model=arguments.model, llm_config=arguments.llm_config)
