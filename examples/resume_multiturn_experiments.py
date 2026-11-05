"""Opt-in long-run, multi-start and failure experiments for multi-turn Resume."""

import argparse
import hashlib
import json
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from examples import resume_restore_validation as restoration
from examples.resume_multiturn_validation import (
    INSTRUCTION,
    observe_run,
    recover_nodes,
    validate_source_case,
)
from examples.resume_record_validation import SOURCES, SourceCase
from palinreplay.core.models import Node, RunEnd
from palinreplay.llm import LLMConfig, mini_model_name
from palinreplay.record.calls import RecordingError
from palinreplay.resume import (
    decide_resume_start,
    restore_resume_start,
    run_resume_steps,
    select_resume_start,
)
from palinreplay.resume.materials import load_resume_materials
from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.io import outside_store, write_json
from palinreplay.storage.store import RecordStore

REAL_INSTRUCTION = "Continue the original task using actual tool results. When finished, use the native submission command."


@contextmanager
def audit_source(spec, client, output, result, services):
    before = restoration.file_inventory(spec.store.parent)
    nodes_before = restoration.node_inventory(spec.store)
    materials = load_resume_materials(select_resume_start(spec.store, spec.node_id, model="M", max_steps=100))
    pool_before = restoration.pool_audit(client, materials.snapshot)
    manifest = hashlib.sha256(services.read_bytes()).hexdigest()
    try:
        yield
    finally:
        after = restoration.file_inventory(spec.store.parent)
        nodes_after = restoration.node_inventory(spec.store)
        pool_after = restoration.pool_audit(client, materials.snapshot)
        write_json(output / "source-files-before.json", before)
        write_json(output / "source-files-after.json", after)
        result["source_audit"] = {
            "files_unchanged": before == after, "nodes_unchanged": nodes_before == nodes_after,
            "pool_source_unchanged": pool_before["source"] == pool_after["source"],
            "pool_workspaces_unchanged": pool_before["workspaces"] == pool_after["workspaces"],
            "manifest_unchanged": manifest == hashlib.sha256(services.read_bytes()).hexdigest(),
            "pool_before": pool_before, "pool_after": pool_after,
        }
        assert all(result["source_audit"][key] for key in (
            "files_unchanged", "nodes_unchanged", "pool_source_unchanged",
            "pool_workspaces_unchanged", "manifest_unchanged",
        ))


def run_real100(output, services, llm_config, *, model=None):
    spec = SOURCES[0]
    output, services = Path(output).absolute(), Path(services).resolve()
    outside_store(spec.store.parent, output)
    credentials = LLMConfig.load(llm_config)
    model = mini_model_name(model or credentials.model)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    entry = {"status": "in_progress", "source_store": str(spec.store), "source_run_id": spec.run_id,
             "source_node_id": spec.node_id, "source_index": spec.index, "max_steps": 100,
             "model": model, "instruction": REAL_INSTRUCTION, "output": str(output),
             "full_requests_verified": False}
    started, client = time.monotonic(), None
    try:
        client = connect_docker(600)
        with audit_source(spec, client, output, entry, services):
            start = select_resume_start(spec.store, spec.node_id, model=model, max_steps=100, run_id=spec.run_id)
            run_error = None
            with observe_run(entry, output, controlled=False) as holder:
                try:
                    with restore_resume_start(start, output / "session", client=client,
                                              services_config=services) as session:
                        holder["session"] = session
                        entry.update(container_id=session.executor._container.id,
                                     source_messages=session.prefix.messages)
                        assert [item["name"] for item in entry["service_starts"]] == ["A", "B"]
                        first = decide_resume_start(session, instruction=REAL_INSTRUCTION, llm_config=llm_config)
                        run_resume_steps(session, first, llm_config=llm_config)
                except Exception as error:
                    run_error = error
            report = restoration.checked_report(client, output / "session")
            recording = report.get("recording", {})
            nodes, ends = (), ()
            store_path = output / "session/record"
            if store_path.exists():
                with RecordStore.open(store_path, read_only=True) as store:
                    nodes, ends = store.list(Node), store.list(RunEnd)
                    assert store.check_integrity().ok and len(ends) == 1
                    assert [node.index for node in nodes] == list(range(21, 21 + len(nodes)))
                    for number, node in enumerate(nodes):
                        assert node.parent_node_id == (nodes[number - 1].node_id if number else None)
                    details = store.read_json(ends[0].details)
                    entry["run_end_details"] = details
                    if details.get("agent_exit_status") == "Submitted":
                        entry["submission"] = details["final_control_context"]["messages"][-1]["extra"]["submission"]
                        entry["submission_round"] = entry["counts"]["model_requests"]
            if recording:
                assert recording["total_model_requests"] == entry["counts"]["model_requests"]
                assert recording["tool_executions"] == entry["counts"]["tool_executions"]
                assert recording["complete_nodes"] == len(nodes)
            requests = entry["counts"]["model_requests"]
            verified_http = entry["verified_http_rounds"]
            decisions = recording.get("decisions", report.get("decision", {}).get("decisions", 0))
            entry.update(status="fault" if report["status"] == "failed" else "passed",
                         recording=recording, model_requests=requests, actual_rounds=len(nodes),
                         successful_decisions=decisions,
                         complete_nodes=len(nodes), nodes=[node.model_dump(mode="json") for node in nodes],
                         last_complete_node=nodes[-1].model_dump(mode="json") if nodes else start.node.model_dump(mode="json"),
                         branch_run_id=recording.get("run_id"), stop_reason=recording.get("stop_reason", "failed"),
                         failure_stage=recording.get("failure_stage", report.get("decision", {}).get("error", {}).get("category")),
                         cleanup_confirmed=report["cleanup_confirmed"])
            # Resume or the SDK can wrap an observer assertion as a model error.
            if len(entry["requests"]) != requests:
                raise AssertionError("full model request verification failed") from run_error
            if len(verified_http) != entry["http_requests"]:
                raise AssertionError("HTTP request verification failed") from run_error
            entry["full_requests_verified"] = requests > 0 and set(verified_http) == set(range(1, requests + 1))
            if isinstance(run_error, AssertionError):
                raise run_error
            if entry["status"] == "passed" and not entry["full_requests_verified"]:
                raise AssertionError("missing verified HTTP request for a successful live run")
            if run_error is not None:
                entry["fault"] = {"type": type(run_error).__name__, "error": str(run_error)}
            if entry["stop_reason"] == "budget_exhausted":
                assert requests == len(nodes) == 100 and nodes[-1].index == 120
            if entry["stop_reason"] in ("submitted", "incomplete_tool_group"):
                assert "submission" in entry
    except BaseException as error:
        entry.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        if client is not None:
            client.close()
        entry["duration_sec"] = time.monotonic() - started
        write_json(output / "acceptance.json", entry)
    print(json.dumps({key: entry[key] for key in (
        "status", "model_requests", "successful_decisions", "actual_rounds", "stop_reason", "duration_sec",
    )}))
    return entry


def run_starts(output, services):
    output, services = Path(output).absolute(), Path(services).resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    base = SOURCES[0]
    summary = {"status": "in_progress", "source_run_id": base.run_id, "cases": {}}
    started = time.monotonic()
    try:
        for index, names in ((1, ()), (12, ("A",)), (31, ())):
            start = select_resume_start(base.store, f"C{index}", run_id=base.run_id,
                                        model="openai/resume-controlled", max_steps=2)
            materials = load_resume_materials(start)
            spec = SourceCase(f"c{index}", base.store, base.run_id, start.node.node_id,
                              start.node.state_id, index, len(materials.context["messages"]))
            entry = validate_source_case(spec, output / spec.name, max_steps=2,
                                         services_config=services, expected_services=names)
            summary["cases"][spec.name] = {key: entry[key] for key in (
                "source_run_id", "source_node_id", "branch_run_id", "container_id", "marker", "counts",
                "request_message_counts", "node_message_counts", "service_starts", "nodes", "cleanup_confirmed",
            )}
            write_json(output / "starts.json", summary, replace=True)
        assert len({item["branch_run_id"] for item in summary["cases"].values()}) == 3
        assert len({item["container_id"] for item in summary["cases"].values()}) == 3
        for name, item in summary["cases"].items():
            others = [other["marker"] for key, other in summary["cases"].items() if key != name]
            recovered = recover_nodes(output / name, output / (name + "-recovery"),
                                       indices=(item["nodes"][-1]["index"],), absent_markers=others)
            item.update(status="passed", recovery=recovered)
        summary["status"] = "passed"
    except BaseException as error:
        summary.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        summary["duration_sec"] = time.monotonic() - started
        write_json(output / "starts.json", summary, replace=True)
    return summary


def run_faults(output, services):
    output, services = Path(output).absolute(), Path(services).resolve()
    spec = SOURCES[0]
    outside_store(spec.store.parent, output)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    config = output / "controlled-llm.toml"
    config.write_text('[llm]\nmodel="openai/resume-controlled"\napi_key="controlled-no-network"\n'
                      'base_url="https://controlled.invalid/v1"\n')
    summary = {"status": "in_progress", "cases": {}}
    started, client = time.monotonic(), None
    try:
        client = connect_docker(600)
        with audit_source(spec, client, output, summary, services):
            for fault in ("model", "tool_interrupt", "snapshot", "after_publication"):
                destination = output / fault
                destination.mkdir()
                entry = {"status": "in_progress", "source_store": str(spec.store),
                         "source_run_id": spec.run_id, "source_node_id": spec.node_id,
                         "source_index": spec.index, "max_steps": 4, "instruction": INSTRUCTION,
                         "marker": "/testbed/.resume-multiturn-fault-" + uuid4().hex,
                         "expected_fault": fault}
                start = select_resume_start(spec.store, spec.node_id, run_id=spec.run_id,
                                            model="openai/resume-controlled", max_steps=4)
                try:
                    with observe_run(entry, destination, fault=fault) as holder:
                        with restore_resume_start(start, destination / "session", client=client,
                                                  services_config=services) as session:
                            holder["session"] = session
                            entry.update(container_id=session.executor._container.id,
                                         source_messages=session.prefix.messages)
                            first = decide_resume_start(session, instruction=INSTRUCTION, llm_config=config)
                            run_resume_steps(session, first, llm_config=config)
                except BaseException as error:
                    expected = {"model": RuntimeError, "tool_interrupt": KeyboardInterrupt,
                                "snapshot": RecordingError, "after_publication": RecordingError}[fault]
                    if not isinstance(error, expected):
                        raise
                    entry["expected_error"] = {"type": type(error).__name__, "error": str(error)}
                else:
                    raise AssertionError("injected second-round failure did not stop Resume")
                report = restoration.checked_report(client, destination / "session")
                recording = report["recording"]
                expected_nodes = 2 if fault == "after_publication" else 1
                assert report["status"] == "failed" and recording["complete_nodes"] == expected_nodes
                assert recording["total_model_requests"] == entry["counts"]["model_requests"] == 2
                assert recording["stop_reason"] == ("interrupted" if fault == "tool_interrupt" else "failed")
                with RecordStore.open(destination / "session/record", read_only=True) as store:
                    nodes = store.list(Node)
                    end, = store.list(RunEnd)
                    assert len(nodes) == expected_nodes and end.last_node_id == nodes[-1].node_id
                    assert store.check_integrity().ok
                entry.update(status="passed", observed_status=report["status"], recording=recording,
                             branch_run_id=nodes[-1].run_id, nodes=[n.model_dump(mode="json") for n in nodes],
                             marker_lines=entry["actions"][expected_nodes - 1]["output"].splitlines(),
                             cleanup_confirmed=True)
                write_json(destination / "acceptance.json", entry)
                recovery = recover_nodes(destination, output / (fault + "-recovery"),
                                          indices=tuple(node.index for node in nodes))
                summary["cases"][fault] = {"status": "passed", "recording": recording,
                                           "counts": entry["counts"], "physical_tool_commands": entry["physical_tool_commands"],
                                           "recovery": recovery}
                write_json(output / "faults.json", summary, replace=True)
        summary["status"] = "passed"
    except BaseException as error:
        summary.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        if client is not None:
            client.close()
        summary["duration_sec"] = time.monotonic() - started
        write_json(output / "faults.json", summary, replace=True)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("real100", "controlled100", "starts", "faults"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--services-config", type=Path, required=True)
    parser.add_argument("--llm-config", type=Path)
    parser.add_argument("--model")
    args = parser.parse_args()
    if args.mode == "real100":
        if args.llm_config is None:
            parser.error("--llm-config is required for the real model experiment")
        run_real100(args.output, args.services_config, args.llm_config, model=args.model)
    elif args.mode == "controlled100":
        validate_source_case(SOURCES[0], args.output, max_steps=100, services_config=args.services_config,
                             expected_services=("A", "B"))
        recover_nodes(args.output, args.output.with_name(args.output.name + "-recovery"), indices=(120,))
    elif args.mode == "starts":
        run_starts(args.output, args.services_config)
    else:
        run_faults(args.output, args.services_config)
