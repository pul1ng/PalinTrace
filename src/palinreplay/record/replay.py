"""Read Replay coverage and usage from local recording facts, never selected history."""

from palinreplay.core.models import Execution, Failure, Node, Operation, RunEnd, Step
from palinreplay.core.replay_outcome import execution_status, failure_evidence
from palinreplay.record.calls import read_call


def replay_execution(store, run_id):
    with store.snapshot():
        return _replay_execution(store, run_id)


def _replay_execution(store, run_id):
    source = store.replay_source(run_id)
    if source is None:
        return None
    nodes = store.list(Node, run_id=run_id)
    steps = {step.step_id: step for step in store.list(Step, run_id=run_id)}
    operations = {op.operation_id: op for op in store.list(Operation, run_id=run_id)}
    supplies, tools, unknown = [], [], []
    for execution in store.list(Execution, run_id=run_id):
        operation = operations[execution.operation_id]
        trace = read_call(store, execution.execution_id)
        if operation.name == "replay.history" and any(
            event.kind == "result" and event.outcome == "returned" for event in trace.events
        ):
            supplies.append({"index": steps[operation.step_id].index,
                             "execution_id": execution.execution_id})
        if operation.kind == "tool" and any(event.kind == "dispatched" for event in trace.events):
            tools.append(execution.execution_id)
            if not any(e.kind == "result" and e.outcome != "unknown" for e in trace.events):
                unknown.append(execution.execution_id)
    selected = source.end.index - source.start.index
    ends = store.list(RunEnd, run_id=run_id)
    status = ends[-1].status if ends else "unfinalized"
    if ends:
        saved = store.read_json(ends[-1].details).get("replay", {})
        status = saved.get("execution_status", status)
    if status == "completed" and len(nodes) < selected:
        status = "stopped"
    evidence = {"faults": [], "cancellations": []}
    for failure in store.list(Failure, run_id=run_id):
        detail = failure_evidence(failure.category, store.read_json(failure.details))
        for key in evidence:
            evidence[key].extend(detail[key])
    status = execution_status(status, **evidence)
    finalization = store.replay_finalization(run_id)
    receipt = finalization["receipt"]
    if receipt is not None:
        for key in evidence:
            evidence[key].extend(receipt[key])
        status = execution_status(receipt["execution_status"], **evidence)
    status = execution_status(status, **evidence,
                              confirmed=False if finalization["status"] == "unconfirmed" else None)
    return {
        "execution_status": status,
        "outcome_evidence": evidence,
        "finalization": finalization,
        "selected_steps": selected, "started_steps": len(steps), "completed_steps": len(nodes),
        "range_complete": len(nodes) == selected,
        "history_usage": {
            "historical_answer_supplies": len(supplies), "supplies": supplies,
            "dispatched_tool_calls": len(tools), "dispatched_tool_execution_ids": tools,
            "unknown_result_execution_ids": unknown,
        },
    }
