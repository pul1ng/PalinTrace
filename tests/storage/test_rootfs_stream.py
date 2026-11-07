import hashlib
import io
import tarfile
from pathlib import Path

import pytest

import palinreplay.storage.rootfs as _rootfs
from palinreplay.diagnostics.metrics import measure
from palinreplay.diagnostics.timing import debug_timing
from tests.storage.test_rootfs_manifest import files, tar_bytes


class Export:
    def __init__(self, data, *, chunk=1024 * 1024, fail=False, close_fail=False):
        self.data, self.chunk = data, chunk
        self.fail, self.close_fail = fail, close_fail
        self.closed = self.eof = False

    def export(self, chunk_size):
        self.iterator = iter(self._chunks())
        return self

    def _chunks(self):
        for pos in range(0, len(self.data), self.chunk):
            yield b""  # Empty transport chunks do not mean EOF.
            yield self.data[pos:pos + self.chunk]
        if self.fail:
            raise OSError("transport failed after tar end markers")
        self.eof = True

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.iterator)

    def close(self):
        self.closed = True
        if self.close_fail:
            raise OSError("close failed")


@pytest.mark.parametrize("chunk", [1, 511, 512, 10240, 1024 * 1024])
@pytest.mark.parametrize("hash_archive", [True, False])
def test_stream_matches_offline_manifest_offsets_and_unchanged_tar(
    tmp_path, chunk, monkeypatch, hash_archive,
):
    members = [
        *files(),
        ("long/" + "x" * 180 + "/中文", tarfile.REGTYPE, b"pax-data", 0o600),
        ("nonutf-\udcff", tarfile.REGTYPE, b"other", 0o644),
        ("empty", tarfile.REGTYPE, b"", 0o644),
        ("forward", tarfile.LNKTYPE, "later", 0o640),
        ("later", tarfile.REGTYPE, b"forward-data", 0o640),
        ("proc/ignored", tarfile.FIFOTYPE, "", 0o600),
    ]
    raw = tar_bytes(members)
    original, staged = tmp_path / "old.tar", tmp_path / "new.tar"
    original.write_bytes(raw)
    expected = _rootfs.inventory(original)
    real_open = Path.open

    def no_reread(path, mode="r", *args, **kwargs):
        if path == staged and mode != "xb":
            pytest.fail("streaming inventory must not reopen the staging tar")
        return real_open(path, mode, *args, **kwargs)

    source = Export(raw, chunk=chunk)
    with monkeypatch.context() as patch, measure() as metrics:
        patch.setattr(Path, "open", no_reread)
        result = _rootfs.export_inventory(source, staged, hash_archive=hash_archive)
    assert (result.logical, result.positions) == expected
    assert staged.read_bytes() == raw
    assert result.sha256 == (hashlib.sha256(raw).hexdigest() if hash_archive else None)
    if not hash_archive:
        assert result.timings["archive_hash_sec"] == 0
    assert result.size == len(raw) and source.closed and source.eof
    assert metrics.counters["read_bytes"] == 0
    assert metrics.counters["exported_bytes"] == metrics.counters["written_bytes"] == len(raw)
    assert all(v >= 0 for v in result.timings.values())


@pytest.mark.parametrize("padding", [0, 512, 1024 * 1024])
@pytest.mark.parametrize("hash_archive", [True, False])
def test_empty_archive_and_zero_padding_are_valid(tmp_path, padding, hash_archive):
    result = _rootfs.export_inventory(
        Export(bytes(1024 + padding)), tmp_path / "tar", hash_archive=hash_archive,
    )
    assert result.logical["entries"] == {} and result.positions == {}


@pytest.mark.parametrize("bad", [
    b"", bytes(512), bytes(1025), tar_bytes(files())[:-1],
    tar_bytes([("file", tarfile.REGTYPE, b"body", 0o644)])[:1536],
    tar_bytes(files()) + tar_bytes(files(b"hidden archive")),
    tar_bytes(files()) + bytes(2 * 1024 * 1024) + b"x" + bytes(511),
    tar_bytes([("file", tarfile.REGTYPE, b"body", 0o644)])[:1536] + b"x" + bytes(511),
])
@pytest.mark.parametrize("hash_archive", [True, False])
def test_stream_checks_two_end_blocks_alignment_and_entire_tail(tmp_path, bad, hash_archive):
    source = Export(bad, chunk=511)
    with pytest.raises((ValueError, tarfile.TarError)):
        _rootfs.export_inventory(source, tmp_path / "tar", hash_archive=hash_archive)
    assert source.closed


@pytest.mark.parametrize("members", [
    [("../escape", tarfile.REGTYPE, b"x", 0o644)],
    [(".wh.deleted", tarfile.REGTYPE, b"", 0o644)],
    [("a", tarfile.REGTYPE, b"x", 0o644), ("a", tarfile.REGTYPE, b"y", 0o644)],
    [("a", tarfile.SYMTYPE, "/outside", 0o777), ("a/child", tarfile.REGTYPE, b"x", 0o644)],
    [("a", tarfile.LNKTYPE, "missing", 0o644)],
    [("a", tarfile.LNKTYPE, "b", 0o644), ("b", tarfile.LNKTYPE, "a", 0o644)],
    [("a", tarfile.LNKTYPE, "proc/x", 0o644), ("proc/x", tarfile.REGTYPE, b"x", 0o644)],
    [("pipe", tarfile.FIFOTYPE, "", 0o600)],
])
@pytest.mark.parametrize("hash_archive", [True, False])
def test_stream_preserves_path_type_and_link_validation(tmp_path, members, hash_archive):
    with pytest.raises(ValueError):
        _rootfs.export_inventory(
            Export(tar_bytes(members)), tmp_path / "tar", hash_archive=hash_archive,
        )
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("fail,close_fail", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("hash_archive", [True, False])
def test_complete_tar_does_not_hide_transport_or_close_failure(
    tmp_path, fail, close_fail, hash_archive,
):
    source = Export(tar_bytes(files()), fail=fail, close_fail=close_fail)
    with pytest.raises(OSError) as caught:
        _rootfs.export_inventory(source, tmp_path / "tar", hash_archive=hash_archive)
    assert source.closed
    if fail:
        assert "transport" in str(caught.value)
    if fail and close_fail:
        assert "close failed" in caught.value.__notes__[0]


@pytest.mark.parametrize("hash_archive", [True, False])
def test_large_file_stream_has_bounded_reads_and_checks_truncated_body(tmp_path, hash_archive):
    raw = tar_bytes([("big", tarfile.REGTYPE, b"x" * (3 * 1024 * 1024 + 13), 0o644)])
    source = Export(raw, chunk=4093)
    result = _rootfs.export_inventory(source, tmp_path / "complete", hash_archive=hash_archive)
    assert result.logical["entries"]["big"]["sha256"] == hashlib.sha256(
        b"x" * (3 * 1024 * 1024 + 13)
    ).hexdigest()
    with pytest.raises((ValueError, tarfile.TarError)):
        _rootfs.export_inventory(
            Export(raw[:1024 * 1024]), tmp_path / "truncated", hash_archive=hash_archive,
        )
    with pytest.raises(ValueError, match="bounded"):
        _rootfs._ExportReader([], io.BytesIO(), tmp_path / "unused").read(-1)


def test_short_write_is_rejected(tmp_path):
    class ShortWrite(io.BytesIO):
        def write(self, value):
            return super().write(value[:-1])

    reader = _rootfs._ExportReader([b"data"], ShortWrite(), tmp_path / "unused")
    with pytest.raises(OSError, match="short write"):
        reader.read(4)


def test_stream_timing_is_bounded_and_never_records_one_span_per_chunk(tmp_path):
    raw = tar_bytes([("big", tarfile.REGTYPE, b"x" * (3 * 1024 * 1024), 0o644)])
    with debug_timing(True, max_spans=10) as timing:
        _rootfs.export_inventory(Export(raw, chunk=1001), tmp_path / "tar")
    report = timing.snapshot()
    assert report["timeline_complete"]
    assert {s["name"] for s in report["spans"]} == {"rootfs.export_inventory", "rootfs.sync_file"}
