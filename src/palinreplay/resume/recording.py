"""Open a branch recording while borrowing the active Resume workspace."""

import base64
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256

from palinreplay.core.interfaces import BoundaryAdapter, Payload
from palinreplay.core.models import Event, Execution, Node, Operation, Run, RunEnd
from palinreplay.core.serialization import json_bytes
from palinreplay.record.recorder import Recorder
from palinreplay.record.state import BtrfsStateBackend
from palinreplay.resume.report import exception_evidence
from palinreplay.storage.io import write_json
from palinreplay.storage.store import RecordStore


def branch_config(session):
    materials = session.materials
    source = {
        "store": str(session.start.source_store),
        "run": materials.run.model_dump(mode="json"),
        "node": materials.node.model_dump(mode="json"),
        "state": materials.state.model_dump(mode="json"),
    }
    # Old derivation permissions belong to the source Run, not this branch.
    config = {key: deepcopy(value) for key, value in materials.config.items()
              if key not in ("replay", "resume")}
    if "model" in config:
        config["model"]["model_name"] = session._decision_evidence["model"]
    config["resume"] = {
        "schema_version": "palinreplay.resume.v1",
        "initial_state_mode": "source-reference-v1",
        "node_numbering": "source-index-v1",
        "model": session.start.model, "max_steps": session.start.max_steps,
        "source": source, "source_sha256": sha256(json_bytes(source)).hexdigest(),
        "raw_materials": {role: base64.b64encode(getattr(materials, f"{role}_payload").data)
                          .decode("ascii") for role in ("config", "context", "input_position")},
    }
    return config


@contextmanager
def open_recording(session, export_agent):
    """Own the Store/Recorder only; session keeps ownership of executor and container."""
    with RecordStore.create(session.output / "record") as store:
        session._record_attempt["store"] = str(store.root)
        recorder = None
        try:
            materials = session.materials
            backend = BtrfsStateBackend(session.executor, runtime=materials.runtime,
                                       public_env_keys=materials.runtime.launch.environment)
            recorder = Recorder(
                store, task_id=materials.run.task_id, agent_name=materials.run.agent_name,
                agent_version=materials.run.agent_version, config=Payload.json(branch_config(session)),
                boundary=BoundaryAdapter(export_agent, backend.capture),
            )
            recorder.start_from_source()
            step = begin_recording_step(session, recorder)
            yield recorder, step
        except BaseException as error:
            if recorder is not None:
                recorder.__exit__(type(error), error, error.__traceback__)
            raise
        else:
            recorder.close()


def begin_recording_step(session, recorder):
    """Begin another step without recreating the branch or finalizing its Run."""
    step = recorder.begin_step()
    session._record_attempt.update(status="recording", store=str(recorder.store.root),
                                   run_id=recorder.run.run_id, step_id=step.step.step_id,
                                   index=step.step.index)
    write_json(session.report_path, session.report, replace=True)
    return step


def _persisted_progress(session):
    attempt = session._record_attempt
    if "store" not in attempt:
        return {"complete_nodes": 0}
    with RecordStore.open(attempt["store"], read_only=True) as store:
        runs = store.list(Run)
        if not runs:
            return {"complete_nodes": 0}
        run_id = attempt.get("run_id", runs[0].run_id)
        nodes = store.list(Node, run_id=run_id)
        tools = {op.operation_id for op in store.list(Operation, run_id=run_id) if op.kind == "tool"}
        executions = {e.execution_id for e in store.list(Execution, run_id=run_id)
                      if e.operation_id in tools}
        events = [event for event in store.list(Event, run_id=run_id)
                  if event.execution_id in executions]
        results = [event for event in events if event.kind == "result"]
        ends = store.list(RunEnd, run_id=run_id)
        progress = {
            "run_id": run_id, "complete_nodes": len(nodes),
            "committed_node_ids": [node.node_id for node in nodes],
            "recorded_tool_dispatches": sum(event.kind == "dispatched" for event in events),
            "tool_results": sum(event.outcome != "unknown" for event in results),
            "unknown_tool_results": sum(event.outcome == "unknown" for event in results),
            "accepted_tools": sum(event.kind == "accepted" for event in events),
            "run_status": ends[-1].status if ends else "unfinalized",
        }
        if nodes:
            node = nodes[-1]
            progress.update(node_id=node.node_id, state_id=node.state_id, index=node.index)
        return progress


def finish_recording(session, primary=None):
    """Retain actual publication facts even when commit or later reporting raises."""
    attempt, pending = session._record_attempt, primary
    try:
        attempt.update(_persisted_progress(session))
    except BaseException as error:
        attempt.update(complete_nodes=None, progress_error=exception_evidence(error))
        if pending is None:
            pending = error
        else:
            pending.add_note(f"Resume progress could not be read: {type(error).__name__}")
    if pending is None:
        attempt["status"] = "incomplete" if attempt.get("run_status") == "incomplete" else "succeeded"
    else:
        attempt["status"] = "failed" if isinstance(pending, Exception) else "interrupted"
        attempt["error"] = exception_evidence(pending)
    try:
        write_json(session.report_path, session.report, replace=True)
    except BaseException as error:
        attempt.update(status="failed", report_error=exception_evidence(error))
        if pending is None:
            pending = error
        else:
            pending.add_note(f"Resume recording report also failed: {type(error).__name__}")
    if primary is None and pending is not None:
        raise pending
