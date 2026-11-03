import os
from copy import deepcopy
from types import SimpleNamespace

import pytest

from palinreplay.replay.compare import compare_exit_codes, compare_tool, is_mtime_only_difference
from palinreplay.sandbox.base import ExecResult
from palinreplay.storage.btrfs.incremental import compare_candidates
from palinreplay.storage.rootfs import SCOPE
from tests.helpers.replay import mtime_difference


@pytest.mark.parametrize("expected,actual,equal", [(0, 0, True), (1, 1, True), (0, 1, False),
                                                   (1, 0, False), (1, 2, False)])
def test_exit_only_policy(expected, actual, equal):
    result = compare_exit_codes(expected, actual)
    assert result == {"policy": "historical-exit-code-v1", "equal": equal,
                      "source_exit_code": expected, "actual_exit_code": actual}


@pytest.mark.parametrize("code", [None, True, False, "0", 0.0])
@pytest.mark.parametrize("side", ["expected", "actual"])
def test_exit_only_policy_rejects_unknown_or_noninteger_status(code, side):
    with pytest.raises(RuntimeError, match="definitive integer"):
        compare_exit_codes(code if side == "expected" else 0, code if side == "actual" else 0)


@pytest.mark.parametrize("old,new,expected", [(0, 1, False), (1, 0, False), (1, 2, False), (3, 3, True)])
def test_exit_status_both_directions_and_equal_known_error(old, new, expected):
    original = SimpleNamespace(raw=b"output", details={"backend": {"decoded": {"returncode": old}}},
                               source={"execution_id": "source"})
    result = compare_tool(original, ExecResult("command", b"output", new, 123))
    assert result["equal"] is expected
    assert result["definitive"]
    assert result["timing"]["actual_duration_sec"] == 123


def test_raw_output_is_compared_without_decoding_or_newline_normalization():
    old = SimpleNamespace(raw=b"a\xff\r\n", details={"backend": {"decoded": {"returncode": 0}}},
                          source={"execution_id": "source"})
    assert not compare_tool(old, ExecResult("cmd", b"a\xff\n", 0, 1))["equal"]
    assert not compare_tool(old, ExecResult("cmd", b"a\xfe\r\n", 0, 1))["equal"]
    assert compare_tool(old, ExecResult("cmd", b"a\xff\r\n", 0, 2))["equal"]
    unknown = compare_tool(old, SimpleNamespace(exit_code=None, output=b"", duration_sec=1))
    assert unknown["status"] == "unknown" and not unknown["definitive"]


def test_incremental_evidence_allows_mtime_but_rejects_content_changes(tmp_path):
    source, actual = tmp_path / "source", tmp_path / "actual"
    for root, timestamp in ((source, 100), (actual, 200)):
        root.mkdir()
        path = root / "file"
        path.write_bytes(b"data")
        os.utime(path, (timestamp, timestamp))
    files = compare_candidates(source, actual, ["file"], SCOPE)
    assert not files["equal"] and is_mtime_only_difference(files)
    (actual / "file").write_bytes(b"changed")
    assert not is_mtime_only_difference(compare_candidates(source, actual, ["file"], SCOPE))


def test_only_mtime_differences_are_allowed_without_altering_evidence():
    files = mtime_difference()
    second = mtime_difference("another")["differences"][0]
    files["differences"].append(second)
    files["different_paths"].append("another")
    files["different_path_count"] = 2
    before = deepcopy(files)
    assert is_mtime_only_difference(files)
    assert files == before and files["equal"] is False


@pytest.mark.parametrize("field,value", [
    ("sha256", "0" * 64), ("size", 5), ("mode", 0o600), ("uid", 1), ("gid", 1),
    ("type", "directory"), ("target", "elsewhere"), ("new_metadata", "changed"),
    ("mtime_sec", None), ("mtime_sec", 100),
])
def test_mtime_mixed_with_critical_or_unknown_changes_is_rejected(field, value):
    files = mtime_difference()
    files["differences"][0]["actual"][field] = value
    assert not is_mtime_only_difference(files)


@pytest.mark.parametrize("side", ["source", "actual"])
def test_added_or_removed_files_are_not_metadata_only(side):
    files = mtime_difference()
    files["differences"][0][side] = None
    assert not is_mtime_only_difference(files)


@pytest.mark.parametrize("field", [
    "different_paths", "different_path_count", "differences", "hardlinks_equal", "hardlink_paths",
])
def test_missing_comparison_evidence_cannot_allow_continuation(field):
    files = mtime_difference()
    del files[field]
    assert not is_mtime_only_difference(files)


@pytest.mark.parametrize("field", ["type", "mode", "uid", "gid", "size", "sha256", "mtime_sec"])
def test_missing_file_evidence_cannot_allow_continuation(field):
    files = mtime_difference()
    for side in ("source", "actual"):
        del files["differences"][0][side][field]
    assert not is_mtime_only_difference(files)


@pytest.mark.parametrize("change", ["links", "links-only", "count", "paths", "duplicate", "empty"])
def test_incomplete_or_inconsistent_differences_are_not_allowlisted(change):
    files = mtime_difference()
    if change in ("links", "links-only"):
        files.update(hardlinks_equal=False, hardlink_paths=["file", "link"])
    if change in ("links-only", "empty"):
        files.update(differences=[], different_paths=[], different_path_count=0)
    if change == "count":
        files["different_path_count"] += 1
    if change == "paths":
        files["different_paths"] = ["another"]
    if change == "duplicate":
        files["differences"] *= 2
        files["different_paths"] *= 2
        files["different_path_count"] = 2
    assert not is_mtime_only_difference(files)
