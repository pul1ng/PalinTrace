"""Execute a pinned range in one evolving workspace, stopping on differences."""

from palinreplay.core.models import State
from palinreplay.replay.compare import compare_tool
from palinreplay.replay.step import execute_step
from palinreplay.storage.btrfs.models import Snapshot


class PartialDifference(Exception):
    def __init__(self, comparison):
        super().__init__("tool differs before the declared group is complete")
        self.comparison = comparison


def node_snapshot(store, node):
    return Snapshot.model_validate(store.read_json(store.get(State, node.state_id).manifest))


def replay_range(recorder, context, executor, materials, compare_files=None):
    plan = materials.plan
    mappings = [{"source_node_id": plan.start.node_id, "source_index": plan.start.index,
                 "node_id": recorder.current_node.node_id, "index": 0}]
    report = {"status": "match", "mappings": mappings, "steps": []}
    for index, history in enumerate(materials.steps):
        previous = recorder.current_node
        tool_comparisons = []
        entry = {"source_node_id": history.node.node_id, "source_index": history.node.index,
                 "tools": tool_comparisons}
        report["steps"].append(entry)

        def on_result(i, tool, call, result, last):
            compared = compare_tool(tool, result)
            compared["execution_id"] = call.execution_id
            tool_comparisons.append(compared)
            if not compared["equal"] and not last:
                raise PartialDifference(compared)

        try:
            node, _ = execute_step(recorder, context, executor, history, on_result=on_result)
        except PartialDifference as error:
            report.update(status="difference", stop_reason="tool-result", incomplete_step=True,
                          first_difference=error.comparison)
            recorder.end_incomplete("Replay tool difference", details={"replay": report})
            return report
        mappings.append({"source_node_id": history.node.node_id, "source_index": history.node.index,
                         "node_id": node.node_id, "index": node.index})
        entry["node_id"] = node.node_id
        if any(not c["equal"] for c in tool_comparisons):
            report.update(status="difference", stop_reason="tool-result", incomplete_step=False,
                          first_difference=next(c for c in tool_comparisons if not c["equal"]))
            return report
        if compare_files is None:
            raise ValueError("Replay requires incremental file comparison")
        files = compare_files(index, previous, node)
        entry["files"] = files
        if not files["equal"]:
            report.update(status="difference", stop_reason="file-state", incomplete_step=False,
                          first_difference=files)
            return report
    return report


def btrfs_comparison(store, executor, source_snapshots):
    def compare(index, previous, node):
        return executor.pool.compare_incremental(
            source_snapshots[index], source_snapshots[index + 1],
            node_snapshot(store, previous), node_snapshot(store, node),
        )
    return compare
