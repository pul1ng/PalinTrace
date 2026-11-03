"""Summarize actual persisted Replay boundaries and unfinished evidence."""

from palinreplay.core.models import Execution, Node, Operation, RunEnd, Step
from palinreplay.record.calls import read_call


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
    return {
        "actual_node_count": len(nodes), "completed_steps": len(nodes),
        "last_node": ({"node_id": nodes[-1].node_id, "index": nodes[-1].index} if nodes else None),
        "incomplete_step": bool(tails), "incomplete_steps": tails,
        "run_end": ends[-1].model_dump(mode="json") if ends else None,
    }
