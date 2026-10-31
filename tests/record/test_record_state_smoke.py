"""Offline tests for the step-1 proof's integrity comparison; no Docker access."""

import io
import tarfile

import pytest

import palinreplay.storage.rootfs as probe


def archive(tmp_path, name, members):
    path = tmp_path / name
    with tarfile.open(path, "w") as output:
        for member_name, kind, value, mode in members:
            item = tarfile.TarInfo(member_name)
            item.mode, item.uid, item.gid, item.mtime = mode, 1234, 2345, 1700000000
            item.type = kind
            if kind == tarfile.REGTYPE:
                item.size = len(value)
                output.addfile(item, io.BytesIO(value))
            else:
                if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                    item.linkname = value
                output.addfile(item)
    return path


def test_manifest_distinguishes_content_permissions_types_and_path_sets(tmp_path):
    members = [("tmp/empty", tarfile.DIRTYPE, "", 0o700),
               ("testbed/a", tarfile.REGTYPE, b"a\x00\xff", 0o640),
               ("root/config", tarfile.REGTYPE, b"outside", 0o600),
               ("testbed/.git/info/exclude", tarfile.REGTYPE, b"ignored", 0o644),
               ("bin/tool", tarfile.REGTYPE, b"unchanged", 0o644)]
    expected = probe.manifest(archive(tmp_path, "before.tar", members))
    changed = [*members[:1], ("testbed/a", tarfile.REGTYPE, b"b\x00\xff", 0o640),
               ("root/config", tarfile.SYMTYPE, "../elsewhere", 0o777),
               ("bin/tool", tarfile.REGTYPE, b"unchanged", 0o751)]
    actual = probe.manifest(archive(tmp_path, "after.tar", changed))
    result = probe.compare(expected, actual)
    assert result["equal"] is False
    assert result["different_paths"] == ["bin/tool", "root/config",
                                         "testbed/.git/info/exclude", "testbed/a"]
    assert expected["entries"]["testbed/a"]["mtime_sec"] == 1700000000
    assert expected["entries"]["root/config"]["uid"] == 1234


def test_hardlinks_compare_independently_of_archive_order_but_not_as_copies(tmp_path):
    one = [("a", tarfile.REGTYPE, b"same", 0o640),
           ("b", tarfile.LNKTYPE, "a", 0o640)]
    two = [("b", tarfile.REGTYPE, b"same", 0o640),
           ("a", tarfile.LNKTYPE, "b", 0o640)]
    copied = [("a", tarfile.REGTYPE, b"same", 0o640),
              ("b", tarfile.REGTYPE, b"same", 0o640)]
    first = probe.manifest(archive(tmp_path, "one.tar", one))
    assert first == probe.manifest(archive(tmp_path, "two.tar", two))
    comparison = probe.compare(first, probe.manifest(archive(tmp_path, "copy.tar", copied)))
    assert comparison["different_path_count"] == 0
    assert comparison["hardlinks_equal"] is False and comparison["equal"] is False


def test_symlinks_are_not_followed_and_runtime_exclusions_are_exact(tmp_path):
    members = [("link", tarfile.SYMTYPE, str(tmp_path / "must-not-be-created"), 0o777),
               ("proc/mem", tarfile.FIFOTYPE, "", 0o600),
               ("etc/hosts", tarfile.REGTYPE, b"runtime", 0o644),
               ("etc/hosts.saved", tarfile.REGTYPE, b"managed", 0o644),
               ("tmp/name\nwith newline", tarfile.REGTYPE, b"managed", 0o644)]
    state = probe.manifest(archive(tmp_path, "links.tar", members))
    assert set(state["entries"]) == {"link", "etc/hosts.saved", "tmp/name\nwith newline"}
    assert not (tmp_path / "must-not-be-created").exists()


@pytest.mark.parametrize("members", [
    [("../escape", tarfile.REGTYPE, b"bad", 0o644)],
    [("/absolute", tarfile.REGTYPE, b"bad", 0o644)],
    [("a", tarfile.REGTYPE, b"one", 0o644), ("./a", tarfile.REGTYPE, b"two", 0o644)],
    [("a", tarfile.LNKTYPE, "missing", 0o644)],
    [("a", tarfile.LNKTYPE, "b", 0o644), ("b", tarfile.LNKTYPE, "a", 0o644)],
    [("tmp/pipe", tarfile.FIFOTYPE, "", 0o600)],
])
def test_invalid_or_unsupported_managed_archive_is_rejected(tmp_path, members):
    with pytest.raises(ValueError):
        probe.manifest(archive(tmp_path, "invalid.tar", members))


def test_truncated_tar_is_not_accepted_as_a_complete_state(tmp_path):
    path = archive(tmp_path, "truncated.tar", [("a", tarfile.REGTYPE, b"x" * 2048, 0o644)])
    path.write_bytes(path.read_bytes()[:1536])
    with pytest.raises((ValueError, tarfile.ReadError)):
        probe.manifest(path)
