import os

from palinreplay.storage.btrfs import links


def test_alias_closure_across_both_snapshots(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for root in (a, b):
        (root / "file").write_text("same")
        os.link(root / "file", root / "alias\nname")
    os.link(b / "file", b / "third")
    calls = []

    def lookup(root, inode):
        calls.append((root, inode))
        return [p.name for p in root.iterdir() if p.lstat().st_ino == inode]

    monkeypatch.setattr(links, "inode_paths", lookup)
    assert links.close_links((a, b), ["file"]) == ["alias\nname", "file", "third"]
    assert len(calls) == 2
