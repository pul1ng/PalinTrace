"""Opt-in Resume stage 5 acceptance against four fixed, existing Btrfs Records.

The model response is controlled; restoration, tool execution, admission, snapshot
publication and recovery use the actual package entry points and Docker backend.
"""

import argparse
import hashlib
import json
import os
import shlex
import time
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from examples import resume_restore_validation as restoration
from examples.resume_decision_validation import ZERO_COUNTS, decision_boundary
from palinreplay.agents.mini import load_native
from palinreplay.core.errors import RecordUsageError
from palinreplay.core.models import Execution, Node, Operation
from palinreplay.record.calls import RecordingError, read_call
from palinreplay.record.restore import load_restore_materials
from palinreplay.record.state import BtrfsStateBackend
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.resume import (
    decide_resume_start,
    record_resume_step,
    restore_resume_start,
    select_resume_start,
)
from palinreplay.resume.materials import load_resume_materials
from palinreplay.resume.record_context import ResumeRecordContext
from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.io import outside_store, write_json
from palinreplay.storage.store import RecordStore


@dataclass(frozen=True)
class SourceCase:
    name: str
    store: Path
    run_id: str
    node_id: str
    state_id: str
    index: int
    messages: int


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("PALIN_VALIDATION_DATA", PROJECT_ROOT / "data")).expanduser()
TEN = DATA / "btrfs-ten-direct-20261030-223433/results"
SOURCES = (
    SourceCase("c20", PROJECT_ROOT / "outputs/record/261102_150636_service-overlap-40/record",
               "a9261e45064b44029fa21e2cd2c3635e", "c18d313c6bb04ac98b1ebba48f145ba3",
               "1379ebee3b81426494c547ce5676c1a7", 20, 42),
    SourceCase("astropy_initial", TEN / "astropy__astropy-14182/run/record",
               "fb0e1125841c4877822043bac426d330", "42a6f209f15b4aa4be8d0c39b94e7839",
               "4e391681190e479ca9b0e978ecc99094", 0, 2),
    SourceCase("django_middle", TEN / "django__django-10914/run/record",
               "545c3c7c0db2402ab22d0a54c2f57e1a", "4ea52cca2a3a40169cd1c4f1d86c2571",
               "dfa32f03bbb04da2b550428fd055b97a", 1, 4),
    SourceCase("astropy_long", DATA / "btrfs-100steps-20261030-200601/record",
               "577a3cd21a1344beb69e3fceb4cb8695", "d525dddcf31247b8be8eeeb70948d5bf",
               "0a4a34363b464e30aef12a38a658e857", 22, 49),
)
BOUNDARIES = ("guided", "nonzero", "interrupt", "adoption_failure", "snapshot_failure", "closed", "last_parent")


def controlled_response(marker, text, *, exit_code=0):
    from litellm import ModelResponse

    commands = [f"printf '%s\\n' {shlex.quote(text)} >> {shlex.quote(marker)}",
                f"cat {shlex.quote(marker)}" + (f"; exit {exit_code}" if exit_code else "")]
    return ModelResponse(id="resume-stage5-" + uuid4().hex, model="resume-controlled", created=1,
                         choices=[{"index": 0, "finish_reason": "tool_calls", "message": {
                             "role": "assistant", "content": "Append one marker, then read it.",
                             "tool_calls": [{"id": f"resume-tool-{index + 1}", "type": "function",
                                             "function": {"name": "bash", "arguments": json.dumps({"command": command})}}
                                            for index, command in enumerate(commands)],
                         }}], usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


@contextmanager
def recording_boundary(session, counts, actions, *, fault=None):
    native = load_native()
    actual_execute = RestoredExecutor.execute
    actual_capture = BtrfsStateBackend.capture
    actual_commit = RecordStore.commit_node

    def execute(executor, request):
        assert executor is session.executor
        assert executor._container.id == session.restored.container.id
        if fault == "interrupt" and counts["tool_executions"] == 1:
            raise KeyboardInterrupt("acceptance interrupt before second command")
        counts["tool_executions"] += 1
        result = actual_execute(executor, request)
        actions.append({"request": deepcopy(request), "container_id": executor._container.id,
                        "output": result.text, "exit_code": result.exit_code,
                        "duration_sec": result.duration_sec})
        return result

    def capture(backend, *args):
        counts["snapshot_attempts"] += 1
        if fault == "snapshot_failure":
            raise RuntimeError("acceptance failure before snapshot")
        counts["file_snapshots"] += 1
        return actual_capture(backend, *args)

    def commit(store, node):
        value = actual_commit(store, node)
        counts["complete_nodes"] += 1
        return value

    def no_model(**kwargs):
        counts["recording_model_requests"] += 1
        raise AssertionError("recording requested another model response")

    def fail_adoption(*args):
        raise RuntimeError("acceptance result admission failure")

    with ExitStack() as stack:
        stack.enter_context(patch.object(RestoredExecutor, "execute", execute))
        stack.enter_context(patch.object(BtrfsStateBackend, "capture", capture))
        stack.enter_context(patch.object(RecordStore, "commit_node", commit))
        stack.enter_context(patch.object(native.litellm, "completion", no_model))
        if fault == "adoption_failure":
            stack.enter_context(patch.object(ResumeRecordContext, "accept_results", fail_adoption))
        yield


def probe(session, checks, command):
    result = session.executor.exec(command)
    checks.append({"container_id": session.executor._container.id, "command": command,
                   "output": result.text, "exit_code": result.exit_code})
    assert result.exit_code == 0, checks[-1]
    return result.text


def expect_rejection(session, decision):
    try:
        record_resume_step(session, decision)
    except RecordUsageError:
        return
    raise AssertionError("invalid or consumed decision was accepted")


def branch_evidence(path, decision, public_request, *, complete):
    with RecordStore.open(path) as store:
        nodes = store.list(Node)
        assert len(nodes) == int(complete)
        operations = [op for op in store.list(Operation) if op.kind == "tool"]
        assert [op.tool_call_id for op in operations] == [action["tool_call_id"] for action in decision.actions]
        records = []
        for operation in operations:
            execution_id = next(e.execution_id for e in store.list(Execution)
                                if e.operation_id == operation.operation_id)
            trace = read_call(store, execution_id)
            request = store.read_json(trace.execution.request)
            expected = next(action for action in decision.actions if action["tool_call_id"] == operation.tool_call_id)
            assert request["action"] == expected
            result = next(event for event in trace.events if event.kind == "result")
            known = result.outcome != "unknown"
            records.append({"tool_call_id": operation.tool_call_id, "request": request,
                            "phase": trace.phase, "outcome": result.outcome,
                            "output": store.read_bytes(result.payload).decode("utf-8", "replace") if known else None,
                            "exit_code": store.read_json(result.details)["backend"]["decoded"]["returncode"] if known else None})
        assert store.check_integrity().ok
        context = store.read_json(nodes[0].context) if nodes else None
        if nodes:
            assert store.get_node(nodes[0].node_id) == nodes[0]
            model = read_call(store, nodes[0].model_execution_id)
            assert store.read_json(model.events[1].payload) == decision.raw_response
            assert store.read_json(model.execution.request) == public_request
        return nodes, records, context


def run_case(spec, destination, client, llm_config, entry, *, services=None, instruction=None, fault=None, exit_code=0):
    destination.mkdir()
    start = select_resume_start(spec.store, spec.node_id, run_id=spec.run_id,
                                model="openai/resume-controlled", max_steps=1)
    assert (start.node.index, start.node.state_id) == (spec.index, spec.state_id)
    token = uuid4().hex
    marker, text = f"/testbed/.resume-stage5-{token}", f"{spec.name}:{token}"
    response = controlled_response(marker, text, exit_code=exit_code)
    counts = dict.fromkeys((*ZERO_COUNTS, "model_requests", "http_requests", "recording_model_requests", "snapshot_attempts"), 0)
    actions, checks = [], []
    entry.update(source_store=str(spec.store), source_run_id=spec.run_id, source_node_id=spec.node_id,
                 source_state_id=spec.state_id, source_index=spec.index, output=str(destination),
                 marker=marker, marker_text=text, counts=counts, actions=actions, probes=checks,
                 expected_outcome="incomplete" if fault else "complete")
    with restore_resume_start(start, destination / "session", client=client, services_config=services) as session:
        assert session.report["r1"]["status"] == "passed"
        assert len(session.prefix.messages) == spec.messages
        assert session.input_position["next_message"] == spec.messages
        prefix = deepcopy(session.prefix.context)
        container_id = session.executor._container.id
        entry["container_id"] = container_id
        if services is not None:
            preparation = json.loads((destination / "session/preparation.json").read_text())
            assert [service["name"] for service in preparation["services"]] == ["A", "B"]
            assert all(service["startup"]["exit_code"] == 0 for service in preparation["services"])
            entry["service_startups"] = 2
        else:
            entry["service_startups"] = 0
        probe(session, checks, "test ! -e " + shlex.quote(marker))
        with decision_boundary(session, counts, controlled=response):
            decision = decide_resume_start(session, instruction=instruction, llm_config=llm_config)
        assert decision.accounting["remaining_steps"] == 0
        assert counts["model_requests"] == 1 and all(counts[key] == 0 for key in ZERO_COUNTS)
        saved_decision = deepcopy(session.report["decision"])
        with recording_boundary(session, counts, actions, fault=fault):
            if spec.name == "c20" and not fault and instruction is None:
                changed = deepcopy(decision)
                changed.actions[0]["command"] += "; echo modified"
                expect_rejection(session, changed)
                assert not (session.output / "record").exists() and counts["tool_executions"] == 0
                assert session.report["decision"] == saved_decision
                entry["modified_rejected"] = True
            try:
                node = record_resume_step(session, decision)
            except BaseException as error:
                expected = {"interrupt": KeyboardInterrupt, "adoption_failure": RuntimeError,
                            "snapshot_failure": RecordingError}.get(fault)
                if expected is None or not isinstance(error, expected):
                    raise
                entry["expected_error"] = {"type": type(error).__name__, "message": str(error)}
            else:
                assert fault is None, "injected failure did not stop recording"
                assert node.index == spec.index + 1 and node.run_id != spec.run_id
                assert node.node_id != spec.node_id
                before = deepcopy(counts)
                expect_rejection(session, decision)
                assert counts == before
                entry["duplicate_rejected"] = True
        assert counts["recording_model_requests"] == 0
        assert counts["tool_executions"] == (1 if fault == "interrupt" else 2)
        assert counts["complete_nodes"] == (0 if fault else 1)
        assert session.prefix.context == prefix and session.report["decision"] == saved_decision
        assert probe(session, checks, "cat " + shlex.quote(marker)) == text + "\n"
        assert probe(session, checks, "wc -l < " + shlex.quote(marker)).strip() == "1"
        assert session.executor._container.id == container_id
        if fault:
            before = deepcopy(counts)
            expect_rejection(session, decision)
            assert counts == before
            entry["failed_decision_not_replayed"] = True
    report = restoration.checked_report(client, destination / "session")
    assert report["status"] == ("failed" if fault else "passed")
    assert report["decision"]["status"] == "succeeded"
    entry.update(cleanup_confirmed=True, recording=report["recording"],
                 complete_nodes=report["recording"]["complete_nodes"])
    nodes, records, context = branch_evidence(destination / "session/record", decision,
                                             saved_decision["request"], complete=not fault)
    entry["tool_records"] = records
    if not fault:
        node, = nodes
        expected_messages = spec.messages + 3 + (instruction is not None)
        assert len(context["messages"]) == expected_messages
        assert context["messages"][:spec.messages] == prefix["messages"]
        assert [message["tool_call_id"] for message in context["messages"][-2:]] == [action["tool_call_id"] for action in decision.actions]
        assert context["messages"][-1]["extra"]["returncode"] == exit_code
        assert records[-1]["output"] == text + "\n" and records[-1]["exit_code"] == exit_code
        recovered_start = select_resume_start(destination / "session/record", node.node_id, run_id=node.run_id, model="M", max_steps=1)
        with restore_resume_start(recovered_start, destination / "recovered", client=client) as recovered:
            assert recovered.executor._container.id != container_id
            assert probe(recovered, checks, "cat " + shlex.quote(marker)) == text + "\n"
            assert probe(recovered, checks, "wc -l < " + shlex.quote(marker)).strip() == "1"
            assert recovered.prefix.messages == context["messages"]
            entry["recovery_container_id"] = recovered.executor._container.id
        restoration.checked_report(client, destination / "recovered")
        entry.update(branch_run_id=node.run_id, branch_node_id=node.node_id, branch_state_id=node.state_id,
                     branch_index=node.index, message_count=expected_messages, restored=True)
    else:
        assert not nodes and records[0]["outcome"] == "returned"
        assert report["recording"]["tool_results"] == (1 if fault == "interrupt" else 2)
    entry["status"] = "passed"


def closed_case(spec, destination, client, llm_config, entry):
    destination.mkdir()
    start = select_resume_start(spec.store, spec.node_id, run_id=spec.run_id, model="openai/resume-controlled", max_steps=1)
    counts = dict.fromkeys((*ZERO_COUNTS, "model_requests", "http_requests"), 0)
    response = controlled_response("/testbed/.never-executed-" + uuid4().hex, "closed")
    with restore_resume_start(start, destination / "session", client=client) as session:
        with decision_boundary(session, counts, controlled=response):
            decision = decide_resume_start(session, llm_config=llm_config)
        saved = deepcopy(session.report["decision"])
    expect_rejection(session, decision)
    assert not (session.output / "record").exists() and session.report["decision"] == saved
    restoration.checked_report(client, destination / "session")
    entry.update(status="passed", model_requests=1, tool_executions=0, complete_nodes=0,
                 cleanup_confirmed=True, closed_rejected=True, output=str(destination))


def all_cases_passed(cases):
    successful = {spec.name for spec in SOURCES} | {"guided", "nonzero", "last_parent"}
    required = {spec.name for spec in SOURCES} | set(BOUNDARIES)
    if not required <= cases.keys() or any(
        cases[name].get("status") != "passed" or not cases[name].get("cleanup_confirmed")
        for name in required
    ):
        return False
    for name in successful:
        entry = cases[name]
        counts = entry.get("counts", {})
        if (not entry.get("restored") or not entry.get("duplicate_rejected")
                or entry.get("complete_nodes") != 1
                or any(counts.get(key) != value for key, value in {
                    "model_requests": 1, "recording_model_requests": 0,
                    "tool_executions": 2, "complete_nodes": 1, "file_snapshots": 1,
                }.items())):
            return False
    for name in ("interrupt", "adoption_failure", "snapshot_failure"):
        if (cases[name].get("complete_nodes") != 0
                or not cases[name].get("failed_decision_not_replayed")):
            return False
    return bool(cases["c20"].get("modified_rejected") and cases["closed"].get("closed_rejected"))


def main(output, services_config):
    output, services_config = Path(output).absolute(), Path(services_config).resolve()
    for spec in SOURCES:
        outside_store(spec.store.parent, output)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    llm_config = output / "controlled-llm.toml"
    llm_config.write_text('[llm]\nmodel="openai/resume-controlled"\napi_key="controlled-no-network"\nbase_url="https://controlled.invalid/v1"\n')
    summary = {"status": "in_progress", "stage5_complete": False, "cases": {}, "sources": {},
               "services_config": str(services_config), "additional_passed": 0, "additional_total": 3}
    write_json(output / "acceptance.json", summary)
    before, snapshots = {}, {}
    manifest_digest = hashlib.sha256(services_config.read_bytes()).hexdigest()
    started = time.monotonic()
    client = None
    try:
        client = connect_docker(600)
        for spec in SOURCES:
            before[spec.name] = restoration.file_inventory(spec.store.parent)
            start = select_resume_start(spec.store, spec.node_id, run_id=spec.run_id, model="M", max_steps=1)
            materials = load_resume_materials(start)
            snapshots[spec.name] = materials.snapshot
            summary["sources"][spec.name] = {"nodes_before": restoration.node_inventory(spec.store),
                                               "pool_before": restoration.pool_audit(client, materials.snapshot)}
            if spec.name == "c20":
                with RecordStore.open(spec.store) as store:
                    parent_next = load_restore_materials(store, "C21", run_id=spec.run_id)
                snapshots["parent_c21"] = parent_next.snapshot
                summary["sources"][spec.name]["parent_c21_before"] = restoration.pool_audit(client, parent_next.snapshot)
            write_json(output / f"{spec.name}-files-before.json", before[spec.name])
        def execute(name, spec, **kwargs):
            entry = summary["cases"][name] = {"status": "in_progress"}
            print(f"{name}: real restore, tools, recording and recovery", flush=True)
            try:
                run_case(spec, output / name, client, llm_config, entry, **kwargs)
            except Exception as error:
                entry.update(status="failed", error_type=type(error).__name__, error=str(error))
            write_json(output / "acceptance.json", summary, replace=True)
        for spec in SOURCES:
            execute(spec.name, spec, services=services_config if spec.name == "c20" else None)
        execute("guided", SOURCES[0], services=services_config, instruction="Append and read the branch marker once.")
        execute("nonzero", SOURCES[1], exit_code=7)
        for fault in ("interrupt", "adoption_failure", "snapshot_failure"):
            execute(fault, SOURCES[1], fault=fault)
        entry = summary["cases"]["closed"] = {"status": "in_progress"}
        try:
            closed_case(SOURCES[1], output / "closed", client, llm_config, entry)
        except Exception as error:
            entry.update(status="failed", error_type=type(error).__name__, error=str(error))
        parent = summary["cases"]["astropy_initial"]
        if parent.get("status") == "passed":
            spec = SourceCase("last_parent", output / "astropy_initial/session/record",
                              parent["branch_run_id"], parent["branch_node_id"], parent["branch_state_id"],
                              parent["branch_index"], parent["message_count"])
            with RecordStore.open(spec.store) as store:
                assert len(store.list(Node)) == 1 and not store.find_node_ids(index=spec.index + 1)
            execute("last_parent", spec)
        else:
            summary["cases"]["last_parent"] = {"status": "blocked", "reason": "requires successful astropy_initial branch"}
        for spec in SOURCES:
            source = summary["sources"][spec.name]
            source["pool_after"] = restoration.pool_audit(client, snapshots[spec.name])
            source["nodes_after"] = restoration.node_inventory(spec.store)
            source["unchanged"] = (source["pool_before"]["source"] == source["pool_after"]["source"]
                                   and source["pool_before"]["workspaces"] == source["pool_after"]["workspaces"]
                                   and source["nodes_before"] == source["nodes_after"])
            if spec.name == "c20":
                source["parent_c21_after"] = restoration.pool_audit(client, snapshots["parent_c21"])
                assert source["parent_c21_before"]["source"] == source["parent_c21_after"]["source"]
            assert source["unchanged"], spec.name
        summary["status"] = "passed" if all_cases_passed(summary["cases"]) else "failed"
    except BaseException as error:
        summary.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        if client is not None:
            client.close()
        for spec in SOURCES:
            if spec.name in before:
                after = restoration.file_inventory(spec.store.parent)
                write_json(output / f"{spec.name}-files-after.json", after)
                summary["sources"][spec.name]["files_unchanged"] = before[spec.name] == after
                if before[spec.name] != after:
                    summary["status"] = "failed"
        summary["manifest_unchanged"] = manifest_digest == hashlib.sha256(services_config.read_bytes()).hexdigest()
        if not summary["manifest_unchanged"]:
            summary["status"] = "failed"
        summary["additional_passed"] = sum(summary["cases"].get(spec.name, {}).get("status") == "passed" for spec in SOURCES[1:])
        summary["stage5_complete"] = summary["status"] == "passed" and all_cases_passed(summary["cases"])
        summary["duration_sec"] = time.monotonic() - started
        write_json(output / "acceptance.json", summary, replace=True)
    assert summary["stage5_complete"], {name: case["status"] for name, case in summary["cases"].items()}
    print(json.dumps({"status": "passed", "stage5_complete": True, "additional_passed": 3, "output": str(output)}))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--services-config", required=True, type=Path)
    arguments = parser.parse_args()
    main(arguments.output, arguments.services_config)
