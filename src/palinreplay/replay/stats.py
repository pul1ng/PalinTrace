"""Always-on Replay statistics; historical usage never enters actual model cost."""

from contextlib import contextmanager
from copy import deepcopy
from time import monotonic


class ReplayStats:
    def __init__(self):
        self.data = {
            "inherited_usage": {}, "historical_answer_supplies": 0,
            "historical_steps": 0, "completed_steps": 0,
            "actual_tool_executions": 0, "actual_model_calls": 0, "actual_model_cost": 0,
            "seconds": {name: 0.0 for name in (
                "restore", "r1_validation", "preparation", "execution", "snapshot_commit",
            )},
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
