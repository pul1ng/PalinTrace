"""Execute pinned history, retaining actual evidence and checking exit codes only."""

from palinreplay.core.progress import ProgressCallback, ProgressEvent, ProgressReporter
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
    report.update(status="failed", execution_status="failed", validation_status="not-performed",
                  incomplete_step=False, mappings=[], steps=[], statistics=stats.data)

    def stop():
        report.update(status="difference", execution_status="stopped", stop_reason="exit-code")
        return report

    for index, history in enumerate(materials.steps):
        tools = []
        location = {"source_node_id": history.node.node_id, "source_index": history.node.index}
        entry = {**location, "tools": tools}
        report["steps"].append(entry)
        report["incomplete_step"] = True
        notify(ProgressEvent("replaying", source_step=history.node.index, step=index + 1))

        def on_result(i, tool, call, result, last):
            compared = {
                **compare_exit_codes(tool.details["backend"]["decoded"]["returncode"], result.exit_code),
                **location, "tool_index": i, "execution_id": call.execution_id,
                "source_execution_id": tool.source["execution_id"],
            }
            tools.append(compared)
            if not compared["equal"]:
                report.setdefault("first_difference", compared)
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
        report["incomplete_step"] = False
        notify(ProgressEvent("committed", source_step=history.node.index, step=node.index))
        if any(not tool["equal"] for tool in tools):
            return stop()
    report.update(status="match", execution_status="completed")
    return report
