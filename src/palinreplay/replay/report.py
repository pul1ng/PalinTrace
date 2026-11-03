"""Summarize actual persisted Replay boundaries and unfinished evidence."""

from palinreplay.core.models import Execution, Node, Operation, RunEnd, Step
from palinreplay.core.replay_outcome import execution_status
from palinreplay.record.calls import RecordingError, read_call
from palinreplay.record.replay import replay_execution


def exception_evidence(error):
    """Follow RecordingError's structured causes, including interrupt/error cycles."""
    evidence = {"faults": [], "cancellations": []}
    pending, seen = [error], set()
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if not isinstance(item, RecordingError):
            key = "cancellations" if isinstance(item, KeyboardInterrupt) else "faults"
            evidence[key].append({"error_type": type(item).__name__, "error": str(item)})
        for name in ("__cause__", "recording_error", "backend_error", "diagnostic_error"):
            child = getattr(item, name, None)
            if isinstance(child, BaseException):
                pending.append(child)
    return evidence


def final_execution_status(report):
    evidence = report.get("outcome_evidence", {})
    faults = [*evidence.get("faults", ()), *(report.get(key) for key in (
        "cleanup_errors", "termination_error", "record_report_error", "report_errors", "preparation_error",
    ) if report.get(key))]
    return execution_status(report["execution_status"], faults=faults,
                            cancellations=evidence.get("cancellations") or report.get("cancellation"))


def execution_facts(recorder):
    store, run_id = recorder.store, recorder.run.run_id
    nodes = store.list(Node, run_id=run_id)
    committed = {node.step_id for node in nodes}
    operations = {op.operation_id: op for op in store.list(Operation, run_id=run_id)}
    executions = store.list(Execution, run_id=run_id)
    tails = []
    for step in store.list(Step, run_id=run_id):
        if step.step_id in committed:
            continue
        calls = []
        for execution in executions:
            operation = operations[execution.operation_id]
            if operation.step_id != step.step_id:
                continue
            trace = read_call(store, execution.execution_id)
            calls.append({
                "operation_id": operation.operation_id, "execution_id": execution.execution_id,
                "kind": operation.kind, "tool_call_id": operation.tool_call_id,
                "phase": trace.phase, "request_ref": execution.request.model_dump(mode="json"),
                "events": [event.model_dump(mode="json") for event in trace.events],
            })
        tails.append({"step_id": step.step_id, "index": step.index, "calls": calls})
    ends = store.list(RunEnd, run_id=run_id)
    replay = replay_execution(store, run_id)
    return {
        "actual_node_count": len(nodes), "completed_steps": len(nodes),
        "last_node": ({"node_id": nodes[-1].node_id, "index": nodes[-1].index} if nodes else None),
        "incomplete_step": bool(tails), "incomplete_steps": tails,
        "run_end": ends[-1].model_dump(mode="json") if ends else None,
        **{key: replay[key] for key in ("history_usage", "outcome_evidence")},
    }
