import os
from pathlib import Path

import pytest

from palinreplay.storage.btrfs import links
from palinreplay.storage.btrfs.incremental import compare_candidates
from palinreplay.storage.btrfs.worker import compare, inventory
from palinreplay.storage.rootfs import SCOPE


def test_candidate_states_match_full_comparison(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for root in (a, b):
        (root / "unchanged").write_bytes(bytes(10000))
        (root / "file").write_bytes(b"same")
        os.utime(root / "file", (123, 123))
    (a / "source-only").write_text("source")
    (b / "actual-only").write_text("actual")
    os.utime(b / "file", (124, 124))
    result = compare_candidates(a, b, ["source-only", "actual-only", "file"], SCOPE)
    full = compare(inventory(a, SCOPE), inventory(b, SCOPE))
    assert result["equal"] == full["equal"] is False
    assert result["different_paths"] == full["different_paths"]
    assert result["read_bytes"] == 20
    assert "unchanged" not in result["candidate_paths"]


def test_link_relation_difference_without_entry_difference(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for root in (a, b):
        (root / "one").write_text("same")
        os.utime(root / "one", (123, 123))
    os.link(a / "one", a / "two")
    (b / "two").write_text("same")
    os.utime(b / "two", (123, 123))
    monkeypatch.setattr(links, "inode_paths", lambda root, inode: [
        p.name for p in root.iterdir() if p.lstat().st_ino == inode
    ])
    result = compare_candidates(a, b, ["one"], SCOPE)
    assert not result["equal"] and not result["hardlinks_equal"]
    assert result["different_paths"] == []
    assert result["hardlink_paths"] == ["one", "two"]
    assert result["read_bytes"] == 12


def test_type_replacement_does_not_follow_symlink(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    (a / "dir").mkdir(parents=True)
    b.mkdir()
    (a / "dir" / "child").write_text("inside")
    (b / "dir").symlink_to(a / "dir", target_is_directory=True)
    result = compare_candidates(a, b, ["dir", "dir/child"], SCOPE)
    assert result["different_paths"] == ["dir", "dir/child"]
    assert result["actual_read_bytes"] == 0


def test_root_mode_difference_matches_full_inventory_without_content_reads(tmp_path, monkeypatch):
    source, actual = tmp_path / "source", tmp_path / "actual"
    for root in (source, actual):
        root.mkdir()
        (root / "unchanged").write_bytes(b"not a candidate")
    source.chmod(0o700)
    actual.chmod(0o755)
    full = compare(inventory(source, SCOPE, include_root=True),
                   inventory(actual, SCOPE, include_root=True))
    full_default = compare(inventory(source, SCOPE), inventory(actual, SCOPE))
    assert not full_default["equal"] and full_default["different_paths"] == ["."]

    def forbidden(*args, **kwargs):
        raise AssertionError("root metadata comparison must not read file contents")

    monkeypatch.setattr(Path, "open", forbidden)
    result = compare_candidates(source, actual, ["."], SCOPE)
    assert result["equal"] == full["equal"] is False
    assert result["different_paths"] == full["different_paths"] == ["."]
    assert result["differences"][0]["source"]["mode"] == 0o700
    assert result["differences"][0]["actual"]["mode"] == 0o755
    assert result["read_bytes"] == 0


@pytest.mark.parametrize("attribute", ["st_uid", "st_gid"])
def test_root_owner_difference_is_compared(tmp_path, monkeypatch, attribute):
    source, actual = tmp_path / "source", tmp_path / "actual"
    source.mkdir()
    actual.mkdir()
    lstat = Path.lstat

    def different_owner(path):
        info = lstat(path)
        if path == actual:
            values = list(info)
            values[4 if attribute == "st_uid" else 5] += 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "lstat", different_owner)
    result = compare_candidates(source, actual, ["."], SCOPE)
    assert not result["equal"] and result["different_paths"] == ["."]
    assert result["differences"][0]["source"][attribute[3:]] != result["differences"][0]["actual"][attribute[3:]]


def test_root_timestamp_difference_is_outside_scope(tmp_path):
    source, actual = tmp_path / "source", tmp_path / "actual"
    for root in (source, actual):
        root.mkdir()
        root.chmod(0o755)
    os.utime(source, (100, 100))
    os.utime(actual, (200, 200))
    result = compare_candidates(source, actual, ["."], SCOPE)
    assert result["equal"] and result["candidate_paths"] == ["."]
    assert result["read_bytes"] == 0
