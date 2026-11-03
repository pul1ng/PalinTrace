from palinreplay.replay.stats import ReplayStats


def test_stats_persist_without_display_and_keep_usage_separate():
    stats = ReplayStats()
    context = {"counters": {"cost": 12, "n_calls": 20}}
    stats.inherit(context)
    context["counters"]["cost"] = 99
    for name in ("restore", "r1_validation", "preparation", "execution", "snapshot_commit"):
        with stats.timed(name):
            pass
        assert stats.data["seconds"][name] > 0
    assert stats.data["actual_model_calls"] == stats.data["actual_model_cost"] == 0
    assert stats.data["inherited_usage"]["counters"]["cost"] == 12
    assert stats.data["completed_steps"] == stats.data["historical_steps"] == 0
    assert "content_compare" not in stats.data["seconds"]
    assert "change_discovery" not in stats.data["seconds"]
    assert "candidate_path_count" not in stats.data and "read_bytes" not in stats.data
