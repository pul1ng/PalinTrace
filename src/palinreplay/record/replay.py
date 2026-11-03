"""Read Replay coverage and usage from local recording facts, never selected history."""

from palinreplay.core.models import Execution, Failure, Node, Operation, RunEnd, Step
from palinreplay.record.calls import read_call


def replay_execution(store, run_id):
    source = store.replay_source(run_id)
    if source is None:
        return None
    nodes = store.list(Node, run_id=run_id)
    steps = {step.step_id: step for step in store.list(Step, run_id=run_id)}
    operations = {op.operation_id: op for op in store.list(Operation, run_id=run_id)}
    supplies, tools = [], []
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
    selected = source.end.index - source.start.index
    ends = store.list(RunEnd, run_id=run_id)
    status = ends[-1].status if ends else "unfinalized"
    if ends:
        saved = store.read_json(ends[-1].details).get("replay", {})
        status = saved.get("execution_status", status)
    if status == "completed" and len(nodes) < selected:
        status = "stopped"
    cancelled = status == "cancelled"
    failed = status == "failed"
    for failure in store.list(Failure, run_id=run_id):
        detail = store.read_json(failure.details)
        error = detail.get("error") or detail.get("recording_error")
        error_type = detail.get("error_type") or (error.get("type") if isinstance(error, dict) else None)
        interrupted = error_type in ("KeyboardInterrupt", "builtins.KeyboardInterrupt")
        cancelled |= interrupted
        failed |= failure.category == "cleanup" or not interrupted
    if failed:
        status = "failed"
    elif cancelled:
        status = "cancelled"
    return {
        "execution_status": status,
        "selected_steps": selected, "started_steps": len(steps), "completed_steps": len(nodes),
        "range_complete": len(nodes) == selected,
        "history_usage": {
            "historical_answer_supplies": len(supplies), "supplies": supplies,
            "actual_tool_executions": len(tools), "tool_execution_ids": tools,
        },
    }
