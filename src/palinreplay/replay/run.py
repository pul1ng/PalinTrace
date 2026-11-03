"""Execute a pinned history, retaining comparisons and optionally continuing differences."""

from palinreplay.core.models import State
from palinreplay.core.progress import ProgressCallback, ProgressEvent, ProgressReporter
from palinreplay.replay.compare import compare_exit_codes
from palinreplay.replay.stats import ReplayStats
from palinreplay.replay.step import execute_step
from palinreplay.storage.btrfs.models import Snapshot
from palinreplay.storage.rootfs import SCOPE


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


def preparation_comparison(store, executor, source, source_node, node, *, report):
    """Compare the verified R1 baseline with the read-only, prepared C0."""
    actual = node_snapshot(store, node)
    delta = executor.pool.changes(source, actual)
    files = {
        **executor.pool._file_command(
            "compare_candidates", source=source.name, actual=actual.name,
            candidates=delta["candidates"], scope=SCOPE,
        ),
        "source_node_id": source_node.node_id, "source_index": source_node.index,
        "node_id": node.node_id, "index": node.index,
        "source_snapshot": source.model_dump(mode="json"),
        "actual_snapshot": actual.model_dump(mode="json"), "actual_delta": delta,
    }
    report["preparation_files"] = files
    if not files["equal"]:
        _difference(report, files, "service-preparation")
    return files


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


def btrfs_comparison(store, executor, source_snapshots):
    def compare(index, previous, node):
        return executor.pool.compare_incremental(
            source_snapshots[index], source_snapshots[index + 1],
            node_snapshot(store, previous), node_snapshot(store, node),
        )
    return compare
