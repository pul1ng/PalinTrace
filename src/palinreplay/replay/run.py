"""Execute a pinned range in one evolving workspace, stopping on differences."""

from palinreplay.core.models import State
from palinreplay.replay.compare import compare_tool
from palinreplay.replay.stats import ReplayStats
from palinreplay.replay.step import execute_step
from palinreplay.storage.btrfs.models import Snapshot


class PartialDifference(Exception):
    def __init__(self, comparison):
        super().__init__("tool differs before the declared group is complete")
        self.comparison = comparison


def node_snapshot(store, node):
    return Snapshot.model_validate(store.read_json(store.get(State, node.state_id).manifest))


def _difference(report, comparison, reason):
    comparison["difference_reason"] = reason
    report["validation_status"] = "difference"
    report.setdefault("first_difference", comparison)


def replay_range(recorder, context, executor, materials, compare_files=None, *, report=None, stats=None):
    stats = stats or ReplayStats()
    plan = materials.plan
    mappings = [{"source_node_id": plan.start.node_id, "source_index": plan.start.index,
                 "node_id": recorder.current_node.node_id, "index": 0}]
    if report is None:
        report = {}
    report.update(status="failed", execution_status="failed", validation_status="incomplete",
                  incomplete_step=False, mappings=mappings, steps=[], statistics=stats.data)

    def stop(reason):
        completed = len(mappings) == len(materials.steps) + 1
        report.update(status="difference", execution_status="completed" if completed else "stopped")
        if not completed:
            report["stop_reason"] = reason
        return report

    for index, history in enumerate(materials.steps):
        previous = recorder.current_node
        tool_comparisons = []
        location = {"source_node_id": history.node.node_id, "source_index": history.node.index}
        entry = {**location, "tools": tool_comparisons}
        report["steps"].append(entry)
        report["incomplete_step"] = True

        def on_result(i, tool, call, result, last):
            compared = {**compare_tool(tool, result), **location,
                        "tool_index": i, "execution_id": call.execution_id}
            tool_comparisons.append(compared)
            if not compared["equal"]:
                _difference(report, compared, "tool-result")
                if not last:
                    raise PartialDifference(compared)

        try:
            node, _ = execute_step(recorder, context, executor, history, on_result=on_result, stats=stats)
        except PartialDifference:
            stop("tool-result")
            recorder.end_incomplete("Replay tool difference", details={"replay": report})
            return report
        mappings.append({**location, "node_id": node.node_id, "index": node.index})
        entry["node_id"] = node.node_id
        report["incomplete_step"] = False
        if any(not c["equal"] for c in tool_comparisons):
            return stop("tool-result")
        if compare_files is None:
            raise ValueError("Replay requires incremental file comparison")
        files = {**compare_files(index, previous, node), **location}
        entry["files"] = files
        stats.files(files)
        if not files["equal"]:
            _difference(report, files, "file-state")
            return stop("file-state")
    report.update(status="match", execution_status="completed", validation_status="match")
    return report


def btrfs_comparison(store, executor, source_snapshots):
    def compare(index, previous, node):
        return executor.pool.compare_incremental(
            source_snapshots[index], source_snapshots[index + 1],
            node_snapshot(store, previous), node_snapshot(store, node),
        )
    return compare
