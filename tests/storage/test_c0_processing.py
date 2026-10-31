import hashlib
import io
import random
import tarfile
import tracemalloc
from pathlib import PurePosixPath

import pytest

import palinreplay.storage.rootfs as _rootfs
from tests.storage.test_rootfs_manifest import tar_bytes
from tests.storage.test_rootfs_stream import Export


def test_ancestor_cache_matches_path_semantics_and_never_outlives_an_inventory():
    randomizer = random.Random(20261023)
    names = [".", "a", "a/b", "a/b/c", "a/b/d", "a-b/c", "a/bb/c", "中文/\udcff/x"]
    names += ["/".join(["deep"] * depth + ["leaf"]) for depth in range(1, 50)]
    for _ in range(150):
        selected = randomizer.sample(names, randomizer.randrange(len(names) + 1))
        kinds = {p: randomizer.choice(("directory", "file", "symlink")) for p in selected}
        invalid = any(kinds.get(str(parent), "directory") != "directory"
                      for name in kinds for parent in PurePosixPath(name).parents)
        if invalid:
            with pytest.raises(ValueError, match="non-directory ancestor"):
                _rootfs._check_ancestors(kinds)
        else:
            _rootfs._check_ancestors(kinds)
    _rootfs._check_ancestors({"a/b/c": "file"})  # Missing parents are allowed.
    with pytest.raises(ValueError):
        _rootfs._check_ancestors({"a/b/c": "file", "a": "file"})


@pytest.mark.parametrize("kind,value", [
    (tarfile.REGTYPE, b"body"), (tarfile.SYMTYPE, "/elsewhere"), (tarfile.LNKTYPE, "target"),
])
@pytest.mark.parametrize("parent_last", [False, True])
def test_late_non_directory_ancestors_are_rejected_in_both_scanners(
    tmp_path, kind, value, parent_last,
):
    parent = ("a", kind, value, 0o644)
    children = [("a/b/c", tarfile.REGTYPE, b"child", 0o644),
                ("a/b/d", tarfile.REGTYPE, b"sibling", 0o644)]
    members = [*children, parent] if parent_last else [parent, *children]
    members.append(("target", tarfile.REGTYPE, b"target", 0o644))
    raw = tar_bytes(members)
    path = tmp_path / "offline"
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="non-directory ancestor"):
        _rootfs.inventory(path)
    with pytest.raises(ValueError, match="non-directory ancestor"):
        _rootfs.export_inventory(Export(raw), tmp_path / "stream")


def test_excluded_entries_still_participate_in_archive_ancestry_checks(tmp_path):
    raw = tar_bytes([("proc/x", tarfile.FIFOTYPE, "", 0o600),
                     ("proc", tarfile.SYMTYPE, "/other", 0o777)])
    with pytest.raises(ValueError, match="non-directory ancestor"):
        _rootfs.export_inventory(Export(raw), tmp_path / "stream")


@pytest.mark.parametrize("alias", ["a//b", "./a/b", "a/./b"])
def test_normalized_duplicate_paths_are_rejected_before_caching(tmp_path, alias):
    raw = tar_bytes([("a/b", tarfile.REGTYPE, b"first", 0o644),
                     (alias, tarfile.REGTYPE, b"second", 0o644)])
    with pytest.raises(ValueError, match="duplicate"):
        _rootfs.export_inventory(Export(raw), tmp_path / "stream")


def test_contiguous_reads_return_immutable_bytes_even_with_a_mutable_producer(tmp_path):
    backing = bytearray(b"a" * (2 * _rootfs.STREAM_BUFFER))
    target = io.BytesIO()
    reader = _rootfs._ExportReader([backing], target, tmp_path / "unused")
    assert reader.read(1) == b"a"
    data = reader.read(_rootfs.STREAM_BUFFER)
    backing[:] = b"b" * len(backing)
    assert isinstance(data, bytes) and data == reader.last == b"a" * _rootfs.STREAM_BUFFER
    assert target.getvalue() == b"a" * (_rootfs.STREAM_BUFFER + 1)
    assert reader.digest.hexdigest() == hashlib.sha256(target.getvalue()).hexdigest()


@pytest.mark.parametrize("hash_archive", [False, True])
def test_stream_buffering_remains_bounded_over_many_transport_chunks(tmp_path, hash_archive):
    block = b"x" * (1024 * 1024)

    class Sink:
        def __init__(self):
            self.digest = hashlib.sha256()
            self.size = 0

        def write(self, data):
            self.digest.update(data)
            self.size += len(data)
            return len(data)

    target = Sink()
    reader = _rootfs._ExportReader((block for _ in range(64)), target, tmp_path / "unused",
                                  hash_archive=hash_archive)
    tracemalloc.start()
    try:
        while data := reader.read(_rootfs.STREAM_BUFFER):
            assert type(data) is bytes and len(data) <= _rootfs.STREAM_BUFFER
            assert len(reader.last) <= _rootfs.STREAM_BUFFER and len(reader.pending) <= len(block)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert target.size == reader.size == 64 * len(block)
    assert peak < 4 * 1024 * 1024  # No retention of the 64 MiB stream.
    if hash_archive:
        assert reader.digest.hexdigest() == target.digest.hexdigest()


def test_short_write_on_the_contiguous_branch_is_rejected(tmp_path):
    class Sink:
        def write(self, data):
            return len(data) if len(data) == 1 else len(data) - 1

    reader = _rootfs._ExportReader([b"x" * (2 * _rootfs.STREAM_BUFFER)], Sink(), tmp_path / "unused")
    reader.read(1)
    with pytest.raises(OSError, match="short write"):
        reader.read(_rootfs.STREAM_BUFFER)


@pytest.mark.parametrize("chunk", [_rootfs.STREAM_BUFFER - 1, _rootfs.STREAM_BUFFER,
                                  _rootfs.STREAM_BUFFER + 1, 1024 * 1024])
@pytest.mark.parametrize("fault", ["none", "truncate", "tail", "transport", "close"])
def test_fast_and_fragmented_reads_preserve_tar_bytes_offsets_and_end_checks(tmp_path, chunk, fault):
    raw = tar_bytes([("big", tarfile.REGTYPE, b"x" * (2 * 1024 * 1024 + 17), 0o644)])
    expected_path = tmp_path / "offline"
    expected_path.write_bytes(raw)
    expected = _rootfs.inventory(expected_path)
    if fault == "truncate":
        raw = raw[:-1]
    elif fault == "tail":
        raw += bytes(2 * _rootfs.STREAM_BUFFER) + b"x" + bytes(511)
    source = Export(raw, chunk=chunk, fail=fault == "transport", close_fail=fault == "close")
    if fault != "none":
        with pytest.raises((ValueError, OSError, tarfile.TarError)):
            _rootfs.export_inventory(source, tmp_path / "stream")
    else:
        actual = _rootfs.export_inventory(source, tmp_path / "stream")
        assert (actual.logical, actual.positions) == expected
        assert (tmp_path / "stream").read_bytes() == raw
        assert actual.sha256 == hashlib.sha256(raw).hexdigest()
    assert source.closed
