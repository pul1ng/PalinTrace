import os
from types import SimpleNamespace

import palinreplay.replay.run as replay
from palinreplay.storage.btrfs.incremental import compare_candidates


def test_preparation_retains_all_paths_and_raw_file_attributes(tmp_path, monkeypatch):
    source, actual = tmp_path / "source", tmp_path / "actual"
    source.mkdir()
    actual.mkdir()
    names = ["service.pid", "service.log", "mtime-only"] + [f"file-{i}" for i in range(40)]
    for name in names:
        for root in (source, actual):
            (root / name).write_text("old")
            os.utime(root / name, (100, 100))
        (actual / name).write_text("old" if name == "mtime-only" else "new")
        os.utime(actual / name, (200, 200))
    before = SimpleNamespace(name="checkpoints/source", model_dump=lambda **kwargs: {"name": "source"})
    after = SimpleNamespace(name="checkpoints/prepared", model_dump=lambda **kwargs: {"name": "prepared"})
    monkeypatch.setattr(replay, "node_snapshot", lambda store, node: after)

    def changes(left, right):
        assert left is before and right is after
        return {"complete": True, "candidates": names}

    def compare(operation, **kwargs):
        assert operation == "compare_candidates"
        assert kwargs["source"] == before.name and kwargs["actual"] == after.name
        return compare_candidates(source, actual, kwargs["candidates"], kwargs["scope"])

    executor = SimpleNamespace(pool=SimpleNamespace(changes=changes, _file_command=compare))
    report = {"r1": {"status": "passed"}, "preparation": {"status": "ready"}}
    files = replay.preparation_comparison(
        None, executor, before, SimpleNamespace(node_id="C20", index=20),
        SimpleNamespace(node_id="new-C0", index=0), report=report,
    )
    assert not files["equal"] and files["different_path_count"] == len(names)
    assert set(files["different_paths"]) == set(names)
    assert len(files["differences"]) > 30
    timestamp = next(item for item in files["differences"] if item["path"] == "mtime-only")
    assert timestamp["source"]["sha256"] == timestamp["actual"]["sha256"]
    assert timestamp["source"]["mtime_sec"] == 100 and timestamp["actual"]["mtime_sec"] == 200
    assert report["r1"]["status"] == "passed" and report["preparation"]["status"] == "ready"
    assert report["first_difference"] is files
    assert files["difference_reason"] == "service-preparation"
    assert files["source_index"] == 20 and files["index"] == 0
