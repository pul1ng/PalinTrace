"""Execute pinned history, retaining actual evidence and checking exit codes only."""

from palinreplay.core.models import Step
from palinreplay.core.progress import ProgressCallback, ProgressEvent, ProgressReporter
from palinreplay.record.calls import read_call
from palinreplay.replay.compare import compare_exit_codes
from palinreplay.replay.stats import ReplayStats
from palinreplay.replay.step import execute_step


class PartialDifference(Exception):
    def __init__(self, comparison):
        super().__init__("exit code differs before the declared group is complete")
        self.comparison = comparison


def replay_range(
    recorder, context, executor, materials, *, report=None, stats=None,
    validation_mode="lenient",
    progress: ProgressCallback | None = None,
):
    stats = stats or ReplayStats()
    notify = ProgressReporter(progress)
    if report is None:
        report = {}
    report.update(execution_status="failed", incomplete_step=False,
                  mappings=[], steps=[], actual_node_count=0, completed_steps=0, statistics=stats.data)

    def stop():
        report.update(execution_status="stopped", stop_reason="exit-code")
        return report

    for index, history in enumerate(materials.steps):
        tools = []
        location = {"source_node_id": history.node.node_id, "source_index": history.node.index}
        entry = {**location, "tools": tools}
        report["steps"].append(entry)
        report["position"] = {**location, "index": history.node.index}
        report["incomplete_step"] = True
        notify(ProgressEvent("replaying", source_step=history.node.index, step=index + 1))

        def on_result(i, tool, call, result, last):
            trace = read_call(recorder.store, call.execution_id)
            actual_step = recorder.store.get(Step, trace.operation.step_id)
            compared = {
                **location, "actual_index": actual_step.index, "step_id": actual_step.step_id,
                "tool_index": i, "tool_ordinal": i + 1, "tool_call_id": trace.operation.tool_call_id,
                "operation_id": trace.operation.operation_id, "execution_id": call.execution_id,
                "source_operation_id": tool.source["operation_id"],
                "source_execution_id": tool.source["execution_id"],
                "source_result_ref": tool.source["raw_ref"],
                "request_ref": trace.execution.request.model_dump(mode="json"),
                "result_ref": trace.events[1].payload.model_dump(mode="json"),
                "result_details_ref": trace.events[1].details.model_dump(mode="json"),
                "source_exit_code": tool.details["backend"]["decoded"]["returncode"],
                "actual_exit_code": result.exit_code,
            }
            tools.append(compared)
            report["position"] = {**location, "index": actual_step.index,
                                  "step_id": actual_step.step_id, "tool_index": i}
            compared.update(compare_exit_codes(compared["source_exit_code"], result.exit_code))
            if not compared["equal"]:
                report.setdefault("exit_mismatch", compared)
                report["stop_reason"] = "exit-code"
                if not last:
                    raise PartialDifference(compared)

        try:
            node, _ = execute_step(recorder, context, executor, history, on_result=on_result, stats=stats)
        except PartialDifference:
            stop()
            recorder.end_incomplete("Replay exit code differs", details={"replay": report})
            return report
        report["mappings"].append({**location, "node_id": node.node_id, "index": node.index})
        entry["node_id"] = node.node_id
        for tool in tools:
            tool["node_id"] = node.node_id
        report.update(incomplete_step=False, actual_node_count=index + 1, completed_steps=index + 1)
        report["position"]["node_id"] = node.node_id
        notify(ProgressEvent("committed", source_step=history.node.index, step=node.index))
        if any(not tool["equal"] for tool in tools):
            return stop()
    report.update(execution_status="completed")
    return report
