"""Opt-in multi-turn Resume acceptance with real Docker tools and Btrfs snapshots."""

import argparse
import hashlib
import json
import shlex
import time
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx

from examples import resume_restore_validation as restoration
from examples.resume_decision_validation import semantic_messages
from examples.resume_record_validation import SOURCES, probe
from palinreplay.agents.mini import load_native
from palinreplay.core.models import Execution, Node, Operation, Run, RunEnd
from palinreplay.record.calls import read_call
from palinreplay.record.recorder import Recorder
from palinreplay.record.state import BtrfsStateBackend
from palinreplay.replay import services as service_module
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.resume import (
    decide_resume_start,
    restore_resume_start,
    run_resume_steps,
    select_resume_start,
)
from palinreplay.resume import session as session_module
from palinreplay.resume.materials import load_resume_materials
from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.io import outside_store, write_json
from palinreplay.storage.store import RecordStore

INSTRUCTION = "Continue this branch, preserving and reading the marker written by each round."


def controlled_response(marker, number):
    from litellm import ModelResponse

    quoted = shlex.quote(marker)
    if number == 1:
        command = ("read value < /proc/sys/kernel/random/uuid; "
                   f"printf 'round-1:%s\\n' \"$value\" >> {quoted}; cat {quoted}")
    else:
        command = f"printf 'round-{number}\\n' >> {quoted}; cat {quoted}"
    return ModelResponse(id=f"multiturn-response-{number}", model="resume-controlled", created=1,
                         choices=[{"index": 0, "finish_reason": "tool_calls", "message": {
                             "role": "assistant", "content": f"Append and read branch round {number}.",
                             "tool_calls": [{"id": f"multiturn-tool-{number}", "type": "function",
                                             "function": {"name": "bash", "arguments": json.dumps({"command": command})}}],
                         }}], usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


@contextmanager
def observe_run(entry, output, *, controlled=True, fault=None):
    """Count actual boundaries while leaving restoration, tools and snapshots real."""
    native = load_native()
    original_restore, original_start = session_module.restore_environment, service_module.start_service
    original_execute, original_capture = RestoredExecutor.execute, BtrfsStateBackend.capture
    original_commit, original_branch = RecordStore.commit_node, Recorder.start_from_source
    original_completion, original_send = native.litellm.completion, httpx.Client.send
    holder = {}
    counts = entry["counts"] = dict.fromkeys(("restores", "branch_starts", "model_requests",
                                             "tool_executions", "snapshots", "complete_nodes"), 0)
    entry.update(requests=[], responses=[], actions=[], service_starts=[],
                 http_requests=0, physical_tool_commands=0)

    def restore(*args, **kwargs):
        counts["restores"] += 1
        return original_restore(*args, **kwargs)

    def start(executor, service, journal):
        entry["service_starts"].append({"name": service.name, "container_id": executor._container.id})
        return original_start(executor, service, journal)

    def branch(recorder):
        counts["branch_starts"] += 1
        return original_branch(recorder)

    def complete(**request):
        counts["model_requests"] += 1
        number = counts["model_requests"]
        assert number <= entry["max_steps"], "model request exceeded the new-decision budget"
        public = {key: deepcopy(value) for key, value in request.items() if key not in ("api_key", "api_base")}
        session = holder["session"]
        if number == 1:
            assert session.report["decision"]["request"] == public
            expected = deepcopy(session.prefix.messages)
            if entry["instruction"] is not None:
                expected.append({"role": "user", "content": entry["instruction"]})
        else:
            with RecordStore.open(session.output / "record", read_only=True) as store:
                step_id = session._record_attempt["step_id"]
                model, = [op for op in store.list(Operation) if op.step_id == step_id and op.kind == "model"]
                execution, = [e for e in store.list(Execution) if e.operation_id == model.operation_id]
                assert store.read_json(execution.request) == public
                previous = max(store.list(Node), key=lambda node: node.index)
                expected = store.read_json(previous.context)["messages"]
        assert semantic_messages(public["messages"]) == semantic_messages(expected)
        entry["requests"].append(public)
        write_json(output / f"request-{number:03}.json", public)
        if fault == "model" and number == 2:
            raise RuntimeError("acceptance second-round model failure")
        response = controlled_response(entry["marker"], number) if controlled else original_completion(**request)
        raw = response.model_dump(mode="json")
        entry["responses"].append(raw)
        write_json(output / f"response-{number:03}.json", raw)
        return response

    def execute(executor, request):
        session = holder["session"]
        assert executor is session.executor and executor._container.id == entry["container_id"]
        counts["tool_executions"] += 1
        if fault == "tool_interrupt" and counts["tool_executions"] == 2:
            raise KeyboardInterrupt("acceptance second-round tool interruption")
        entry["physical_tool_commands"] += 1
        result = original_execute(executor, request)
        action = {"request": deepcopy(request), "container_id": executor._container.id,
                  "output": result.text, "exit_code": result.exit_code, "duration_sec": result.duration_sec}
        entry["actions"].append(action)
        write_json(output / f"tool-{counts['tool_executions']:03}.json", action)
        return result

    def capture(backend, *args):
        if fault == "snapshot" and args[1].index == entry["source_index"] + 2:
            raise RuntimeError("acceptance second-round snapshot failure")
        result = original_capture(backend, *args)
        counts["snapshots"] += 1
        return result

    def commit(store, node):
        result = original_commit(store, node)
        counts["complete_nodes"] += 1
        if fault == "after_publication" and node.index == entry["source_index"] + 2:
            raise OSError("acceptance failure after second-node publication")
        return result

    def send(client, request, **kwargs):
        if request.method == "POST" and request.url.path.endswith("/chat/completions"):
            entry["http_requests"] += 1
            body = json.loads(request.content)
            assert semantic_messages(body["messages"]) == semantic_messages(entry["requests"][-1]["messages"])
            write_json(output / f"http-request-{entry['http_requests']:03}.json", body)
        return original_send(client, request, **kwargs)

    with ExitStack() as stack:
        for owner, name, wrapper in (
            (session_module, "restore_environment", restore), (service_module, "start_service", start),
            (Recorder, "start_from_source", branch), (native.litellm, "completion", complete),
            (RestoredExecutor, "execute", execute), (BtrfsStateBackend, "capture", capture),
            (RecordStore, "commit_node", commit), (httpx.Client, "send", send),
        ):
            stack.enter_context(patch.object(owner, name, wrapper))
        yield holder


def audit_branch(store_path, entry):
    """Rebuild every context from actual accepted calls, then compare requests and nodes."""
    with RecordStore.open(store_path, read_only=True) as store:
        run, = store.list(Run)
        source = store.resume_source(run.run_id)
        assert source.start.node_id == entry["source_node_id"]
        assert source.start.run_id == entry["source_run_id"] and run.run_id != source.start.run_id
        nodes = store.list(Node)
        assert [node.index for node in nodes] == list(range(entry["source_index"] + 1,
                                                          entry["source_index"] + entry["max_steps"] + 1))
        expected = deepcopy(entry["source_messages"])
        if entry["instruction"] is not None:
            expected.append({"role": "user", "content": entry["instruction"]})
        message_counts = []
        marker_lines = None
        for number, node in enumerate(nodes, 1):
            assert node.parent_node_id == (nodes[number - 2].node_id if number > 1 else None)
            request = entry["requests"][number - 1]
            assert semantic_messages(request["messages"]) == semantic_messages(expected)
            model = read_call(store, node.model_execution_id)
            assert store.read_json(model.execution.request) == request
            assert store.read_json(model.execution.context) == expected
            raw_result = next(event for event in model.events if event.kind == "result")
            assert store.read_json(raw_result.payload) == entry["responses"][number - 1]
            assert model.phase == "accepted" and len(node.tool_execution_ids) == 1
            expected.extend(store.read_json(model.events[-1].payload)["messages"])
            tool = read_call(store, node.tool_execution_ids[0])
            assert tool.phase == "accepted" and tool.operation.tool_call_id == f"multiturn-tool-{number}"
            result = next(event for event in tool.events if event.kind == "result")
            action = entry["actions"][number - 1]
            assert store.read_bytes(result.payload).decode() == action["output"]
            assert store.read_json(result.details)["backend"]["decoded"]["returncode"] == action["exit_code"] == 0
            expected.extend(store.read_json(tool.events[-1].payload)["messages"])
            context, position = store.read_json(node.context), store.read_json(node.input_position)
            assert context["messages"] == expected
            assert position["next_message"] == len(expected) == position["adoptions"][-1]["stop"]
            assert sum(m.get("content") == entry["instruction"] for m in expected if m["role"] == "user") == int(
                entry["instruction"] is not None
            )
            lines = action["output"].splitlines()
            if number == 1:
                assert len(lines) == 1 and lines[0].startswith("round-1:")
                entry["first_value"] = str(UUID(lines[0].split(":", 1)[1]))
                marker_lines = lines
            else:
                marker_lines.append(f"round-{number}")
                assert lines == marker_lines
            message_counts.append(len(expected))
        end, = store.list(RunEnd)
        assert end.status == "paused" and end.last_node_id == nodes[-1].node_id
        assert store.read_json(end.details)["reason"] == "Resume decision budget exhausted"
        assert store.check_integrity().ok
        entry.update(branch_run_id=run.run_id, nodes=[node.model_dump(mode="json") for node in nodes],
                     request_message_counts=[len(request["messages"]) for request in entry["requests"]],
                     node_message_counts=message_counts, marker_lines=marker_lines,
                     run_end=end.model_dump(mode="json"), store_integrity=True,
                     full_conversation_verified=True, continuous_files_verified=True)


def run_controlled_case(spec, output, client, llm_config, entry, *, max_steps, services=None,
                        instruction=INSTRUCTION, expected_services=()):
    start = select_resume_start(spec.store, spec.node_id, run_id=spec.run_id,
                                model="openai/resume-controlled", max_steps=max_steps)
    entry.update(status="in_progress", source_store=str(spec.store), source_run_id=spec.run_id,
                 source_node_id=spec.node_id, source_index=spec.index, max_steps=max_steps,
                 instruction=instruction, marker="/testbed/.resume-multiturn-" + uuid4().hex,
                 output=str(output), probes=[])
    with observe_run(entry, output) as holder:
        with restore_resume_start(start, output / "session", client=client, services_config=services) as session:
            holder["session"] = session
            entry.update(container_id=session.executor._container.id,
                         source_messages=deepcopy(session.prefix.messages))
            assert len(session.prefix.messages) == spec.messages
            prefix = deepcopy(session.context)
            preparation = session.report["preparation"]
            names = [service["name"] for service in preparation.get("services", [])]
            assert names == list(expected_services)
            assert [item["name"] for item in entry["service_starts"]] == names
            for service in names:
                response = json.loads(probe(session, entry["probes"],
                                            f"/opt/miniconda3/bin/python -B /opt/palin-replay-service-fixture/{service}/request.py"))
                assert response["status"] == 200 and response["body"] == f"PALIN_SERVICE_{service}_OK"
            probe(session, entry["probes"], "test ! -e " + shlex.quote(entry["marker"]))
            first = decide_resume_start(session, instruction=instruction, llm_config=llm_config)
            first_evidence = deepcopy(session._decision_evidence)
            nodes = run_resume_steps(session, first, llm_config=llm_config)
            assert len(nodes) == max_steps
            assert session._decision_evidence == first_evidence and session.context == prefix
            entry["final_file"] = probe(session, entry["probes"], "cat " + shlex.quote(entry["marker"]))
        report = restoration.checked_report(client, output / "session")
        assert report["status"] == "passed"
    assert entry["counts"] == {"restores": 1, "branch_starts": 1, "model_requests": max_steps,
                                "tool_executions": max_steps, "snapshots": max_steps, "complete_nodes": max_steps}
    assert all(action["container_id"] == entry["container_id"] for action in entry["actions"])
    assert all(item["container_id"] == entry["container_id"] for item in entry["service_starts"])
    audit_branch(output / "session/record", entry)
    base = spec.messages + int(instruction is not None)
    assert entry["request_message_counts"] == [base + 2 * number for number in range(max_steps)]
    assert entry["node_message_counts"] == [base + 2 * number for number in range(1, max_steps + 1)]
    assert entry["final_file"].splitlines() == entry["marker_lines"]
    recording = report["recording"]
    assert recording["total_model_requests"] == recording["tool_executions"] == recording["complete_nodes"] == max_steps
    assert recording["stop_reason"] == "budget_exhausted"
    entry.update(recording=recording, cleanup_confirmed=True, status="passed")


def recover_nodes(case_output, output, *, indices=None, absent_markers=()):
    """Restore saved branch nodes after the execution container has been removed."""
    case_output, output = Path(case_output).absolute(), Path(output).absolute()
    case = json.loads((case_output / "acceptance.json").read_text())
    branch_store = case_output / "session/record"
    outside_store(branch_store.parent, output)
    outside_store(Path(case["source_store"]).parent, output)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    if indices is None:
        first, last = case["nodes"][0]["index"], case["nodes"][-1]["index"]
        indices = (first,) if first == last else (first, last)
    result = {"status": "in_progress", "case_output": str(case_output), "output": str(output),
              "branch_run_id": case["branch_run_id"], "source_run_id": case["source_run_id"],
              "source_node_id": case["source_node_id"], "recoveries": []}
    started, client = time.monotonic(), None
    try:
        before = restoration.file_inventory(branch_store.parent)
        client = connect_docker(600)
        restoration.assert_absent(client.containers, case["container_id"])
        result["execution_container_absent"] = True
        containers = {case["container_id"]}
        for index in indices:
            node = next(item for item in case["nodes"] if item["index"] == index)
            destination = output / f"C{index}"
            start = select_resume_start(branch_store, node["node_id"], run_id=case["branch_run_id"],
                                        model="M", max_steps=1)
            with RecordStore.open(branch_store, read_only=True) as store:
                saved_context = store.read_json(start.node.context)
                saved_position = store.read_json(start.node.input_position)
            item = {"index": index, "node_id": node["node_id"], "probes": []}
            with restore_resume_start(start, destination, client=client) as restored:
                container = restored.executor._container.id
                assert container not in containers
                containers.add(container)
                assert restored.materials.run.run_id == case["branch_run_id"]
                assert restored.prefix.messages == saved_context["messages"]
                assert restored.context == saved_context and restored.input_position == saved_position
                assert saved_position["next_message"] == len(saved_context["messages"])
                text = probe(restored, item["probes"], "cat " + shlex.quote(case["marker"]))
                expected = case["marker_lines"][:index - case["source_index"]]
                assert text.splitlines() == expected
                for marker in absent_markers:
                    probe(restored, item["probes"], "test ! -e " + shlex.quote(marker))
                item.update(absent_markers=list(absent_markers),
                            status="passed", container_id=container, file_lines=expected,
                            message_count=len(saved_context["messages"]), input_position=saved_position,
                            branch_run_id=restored.materials.run.run_id)
            report = restoration.checked_report(client, destination)
            assert report["status"] == "passed"
            item["cleanup_confirmed"] = True
            result["recoveries"].append(item)
            write_json(output / f"C{index}-recovery.json", item)
        assert restoration.file_inventory(branch_store.parent) == before
        result.update(status="passed", branch_materials_unchanged=True, cleanup_confirmed=True)
    except BaseException as error:
        result.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        if client is not None:
            client.close()
        result["duration_sec"] = time.monotonic() - started
        write_json(output / "recovery.json", result)
    print(json.dumps({"status": result["status"], "recovered": list(indices), "output": str(output)}))
    return result


def validate_source_case(spec, output, *, max_steps, services_config=None, expected_services=()):
    """Run one controlled source case with its source and pool integrity audits."""
    output = Path(output).absolute()
    services_config = Path(services_config).resolve() if services_config is not None else None
    outside_store(spec.store.parent, output)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    llm_config = output / "controlled-llm.toml"
    llm_config.write_text('[llm]\nmodel="openai/resume-controlled"\napi_key="controlled-no-network"\n'
                          'base_url="https://controlled.invalid/v1"\n')
    entry = {"status": "in_progress", "case": spec.name,
             "services_config": str(services_config) if services_config is not None else None}
    started, client = time.monotonic(), None
    try:
        before = restoration.file_inventory(spec.store.parent)
        nodes_before = restoration.node_inventory(spec.store)
        materials = load_resume_materials(select_resume_start(spec.store, spec.node_id,
                                                               model="M", max_steps=max_steps))
        manifest_digest = hashlib.sha256(services_config.read_bytes()).hexdigest() if services_config is not None else None
        client = connect_docker(600)
        pool_before = restoration.pool_audit(client, materials.snapshot)
        run_controlled_case(spec, output, client, llm_config, entry, max_steps=max_steps,
                            services=services_config, expected_services=expected_services)
        pool_after = restoration.pool_audit(client, materials.snapshot)
        after = restoration.file_inventory(spec.store.parent)
        nodes_after = restoration.node_inventory(spec.store)
        write_json(output / "source-files-before.json", before)
        write_json(output / "source-files-after.json", after)
        entry["source_audit"] = {"nodes_before": nodes_before, "nodes_after": nodes_after,
                                 "pool_before": pool_before, "pool_after": pool_after,
                                 "files_unchanged": before == after,
                                 "nodes_unchanged": nodes_before == nodes_after,
                                 "manifest_unchanged": (manifest_digest == hashlib.sha256(services_config.read_bytes()).hexdigest()
                                                        if services_config is not None else None)}
        assert before == after and nodes_before == nodes_after
        assert pool_before["source"] == pool_after["source"]
        assert pool_before["workspaces"] == pool_after["workspaces"]
        assert services_config is None or entry["source_audit"]["manifest_unchanged"]
    except BaseException as error:
        entry.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        if client is not None:
            client.close()
        entry["duration_sec"] = time.monotonic() - started
        write_json(output / "acceptance.json", entry)
    print(json.dumps({key: entry[key] for key in ("status", "branch_run_id", "counts", "duration_sec", "output")}))
    return entry


def main(output, services_config=None, *, source_name="c20"):
    spec = next(source for source in SOURCES if source.name == source_name)
    return validate_source_case(spec, output, max_steps=4 if source_name == "c20" else 2,
                                services_config=services_config,
                                expected_services=("A", "B") if source_name == "c20" else ())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--services-config", type=Path)
    parser.add_argument("--recover-from", type=Path)
    parser.add_argument("--case", choices=[source.name for source in SOURCES], default="c20")
    args = parser.parse_args()
    if args.recover_from is not None:
        recover_nodes(args.recover_from, args.output)
    else:
        if args.case == "c20" and args.services_config is None:
            parser.error("--services-config is required for the C20 execution")
        main(args.output, args.services_config, source_name=args.case)
