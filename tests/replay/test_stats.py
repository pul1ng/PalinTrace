from palinreplay.replay.stats import ReplayStats


def test_stats_persist_without_debug_and_keep_cost_separate():
    stats = ReplayStats()
    stats.inherit({"counters": {"cost": 12, "n_calls": 20}})
    with stats.timed("execution"):
        stats.data["actual_tool_executions"] += 1
    stats.files({"discovery_sec": 2, "comparison_sec": 3, "candidate_path_count": 4, "read_bytes": 5})
    assert stats.data["actual_model_calls"] == stats.data["actual_model_cost"] == 0
    assert stats.data["inherited_usage"]["counters"]["cost"] == 12
    assert stats.data["seconds"]["execution"] > 0
    assert stats.data["seconds"]["change_discovery"] == 2
    assert stats.data["candidate_path_count"] == 4 and stats.data["read_bytes"] == 5
