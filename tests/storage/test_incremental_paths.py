from palinreplay.storage.btrfs.paths import candidates, project


def test_lifetimes_and_reused_temporary_names(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    (before / "old").write_text("old")
    (after / "new").write_text("new")
    (after / "second").write_text("second")
    ops = [
        {"command": "rename", "path": "old", "to": "tmp"},
        {"command": "rename", "path": "tmp", "to": "new"},
        {"command": "mkfile", "path": "tmp"},
        {"command": "rename", "path": "tmp", "to": "second"},
    ]
    assert candidates(ops, before, after) == ["new", "old", "second"]
    assert project("tmp", ops[3:]) == "second"
    assert project("tmp", ops[:3], reverse=True) is None


def test_directory_move_overwrite_and_modified_child(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    (before / "a").mkdir(parents=True)
    (after / "b").mkdir(parents=True)
    (before / "a" / "file").write_text("old")
    (before / "victim").write_text("replaced")
    (after / "b" / "file").write_text("new")
    (after / "victim").write_text("new object")
    ops = [
        {"command": "rename", "path": "a", "to": "tmp"},
        {"command": "rename", "path": "tmp", "to": "b"},
        {"command": "update_extent", "path": "b/file"},
        {"command": "unlink", "path": "victim"},
        {"command": "mkfile", "path": "o123"},
        {"command": "rename", "path": "o123", "to": "victim"},
    ]
    assert set(candidates(ops, before, after)) == {"a", "a/file", "b", "b/file", "victim"}
