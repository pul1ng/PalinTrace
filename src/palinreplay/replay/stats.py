"""Always-on Replay statistics; historical usage never enters actual model cost."""

from contextlib import contextmanager
from copy import deepcopy
from time import monotonic


class ReplayStats:
    def __init__(self):
        self.data = {
            "inherited_usage": {}, "historical_answer_supplies": 0,
            "actual_tool_executions": 0, "actual_model_calls": 0, "actual_model_cost": 0,
            "seconds": {name: 0.0 for name in (
                "restore", "execution", "snapshot_commit", "change_discovery", "content_compare",
            )}, "candidate_path_count": 0, "read_bytes": 0,
        }

    @contextmanager
    def timed(self, name):
        started = monotonic()
        try:
            yield
        finally:
            self.data["seconds"][name] += monotonic() - started

    def inherit(self, context):
        self.data["inherited_usage"] = deepcopy({
            key: context.get(key) for key in ("counters", "model_accounting", "global_stats")
        })

    def files(self, result):
        self.data["seconds"]["change_discovery"] += result.get("discovery_sec", 0)
        self.data["seconds"]["content_compare"] += result.get("comparison_sec", 0)
        self.data["candidate_path_count"] += result.get("candidate_path_count", 0)
        self.data["read_bytes"] += result.get("read_bytes", 0)
