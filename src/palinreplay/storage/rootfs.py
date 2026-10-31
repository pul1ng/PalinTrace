"""Logical filesystem comparison for Docker rootfs archives; never extract on the host."""

import hashlib
import os
import stat
import tarfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from time import monotonic

from palinreplay.diagnostics.metrics import CountingReader, count, phase, temporary_size, timed
from palinreplay.storage.export_stream import ChunkStats, prefetch_export

SCOPE = {
    "id": "rootfs-files-v1",
    "exclude_subtrees": ["proc", "sys", "dev"],
    "exclude_paths": [".dockerenv", "etc/hosts", "etc/hostname", "etc/resolv.conf"],
    "types": ["file", "directory", "symlink", "hardlink"],
    "attributes": [
        "content",
        "mode",
        "uid",
        "gid",
        "symlink_target",
        "hardlink_groups",
        "file_mtime_seconds",
    ],
}
MANIFEST_VERSION = "palinreplay.rootfs-manifest.v1"
# Larger tar parser buffers repeatedly copy unused bytes around small headers.
# Fixed-input measurements favor 64 KiB; transport chunks stay at 1 MiB.
STREAM_BUFFER = 64 * 1024


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True)
class ExportInventory:
    """Ephemeral inventory of the exact stream written to one private staging file.

    Never persisted as a validation cache. Initial pack publication independently
    reads final saved bytes against this digest before issuing a boundary proof.
    """

    logical: dict
    positions: dict
    sha256: str | None
    size: int
    identity: tuple
    timings: dict
    transport: dict | None = None
    complete_body_pack: bool = False

    def check_path(self, path):
        if _identity(path.lstat()) != self.identity:
            raise ValueError("export staging file changed after streaming inventory")

    def check_artifact(self, ref):
        if ref.sha256 != self.sha256 or ref.size != self.size:
            raise ValueError("published pack differs from the inventoried export stream")


class _ExportReader:
    """Bounded pull/tee reader, retaining only the last parser buffer for EOF checks.

    TarFile can read ahead past its first zero block. Retain that buffer so the
    complete end marker and ALL trailing bytes can be checked without seeking or
    reopening the staged tar. The Docker iterator is still drained through EOF.
    """

    def __init__(self, chunks, target, path, *, hash_archive=True, started=None):
        self.chunks, self.target, self.path = iter(chunks), target, path
        self.pending = memoryview(b"")
        self.last = b""
        self.size = 0
        self.digest = hashlib.sha256() if hash_archive else None
        self.receive_sec = self.write_sec = self.hash_sec = 0.0
        self.started = monotonic() if started is None else started
        self.first_byte_wait_sec = None
        self.stats = ChunkStats()

    def read(self, size):
        if not 0 < size <= STREAM_BUFFER:
            raise ValueError("export reader requires bounded reads")
        if len(self.pending) >= size:
            # Snapshot the pending bytes once; returning a view would let a
            # producer's mutable buffer change the parser's saved EOF evidence.
            data = self.pending[:size].tobytes()
            self.pending = self.pending[size:]
        else:
            buffer, remaining = bytearray(), size
            while remaining:
                if not self.pending:
                    tick = monotonic()
                    try:
                        chunk = next(self.chunks)
                    except StopIteration:
                        break
                    finally:
                        self.receive_sec += monotonic() - tick
                    self.stats.add(chunk)
                    if chunk and self.first_byte_wait_sec is None:
                        self.first_byte_wait_sec = monotonic() - self.started
                    self.pending = memoryview(chunk)
                    if not self.pending:
                        continue
                part = self.pending[:remaining]
                buffer.extend(part)
                remaining -= len(part)
                self.pending = self.pending[len(part):]
            data = bytes(buffer)
        if data:
            tick = monotonic()
            if self.target.write(data) != len(data):
                raise OSError("short write in rootfs export")
            self.write_sec += monotonic() - tick
            if self.digest is not None:
                tick = monotonic()
                self.digest.update(data)
                self.hash_sec += monotonic() - tick
                count(hashed_bytes=len(data))
            self.size += len(data)
            self.last = data
            count(written_bytes=len(data), exported_bytes=len(data))
            temporary_size(self.path, self.size)
        return data

    def finish(self, end):
        start = self.size - len(self.last)
        if not start <= end <= self.size:
            raise ValueError("tar parser end is outside the retained export buffer")
        if self.last[end - start:].strip(b"\0"):
            raise ValueError("unexpected data after tar end marker")
        while data := self.read(STREAM_BUFFER):
            if data.strip(b"\0"):
                raise ValueError("unexpected data after tar end marker")
        if self.size < 1024 or self.size % 512:
            raise ValueError("truncated tar archive")
        if self.size - end < 1024:
            raise ValueError("tar archive has no complete end marker")


@contextmanager
def _export_stream(container, prefetch):
    if prefetch:
        with prefetch_export(container) as stream:
            yield stream
        return
    stream = container.export(chunk_size=1024 * 1024)
    primary = None
    try:
        yield stream
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            if hasattr(stream, "close"):
                stream.close()
        except BaseException as error:
            if primary is None:
                raise
            primary.add_note(f"export stream cleanup also failed: {error!r}")


@timed("rootfs.export_inventory")
def export_inventory(container, path: Path, *, hash_archive=True, prefetch=False) -> ExportInventory:
    """Inventory and persist the SAME incoming tar bytes with bounded buffers.

    Optional prefetch overlaps reception with parsing. Both paths return only
    after tar validation, stream EOF/close, flush, fsync and identity checks.
    hash_archive=False is only for a disposable tar that cannot become a pack;
    every individual file is still hashed, and new pack bodies are checked.
    """
    if type(prefetch) is not bool:
        raise ValueError("prefetch must be boolean")
    started = monotonic()
    with _export_stream(container, prefetch) as stream:
        with path.open("xb") as target:
            os.fchmod(target.fileno(), 0o600)
            reader = _ExportReader(stream, target, path, hash_archive=hash_archive, started=started)
            with tarfile.open(fileobj=reader, mode="r|", bufsize=STREAM_BUFFER) as archive:
                logical, positions = _inventory(archive, locations=True)
                reader.finish(archive.offset)
            tick = monotonic()
            with phase("rootfs.sync_file"):
                target.flush()
                os.fsync(target.fileno())
            sync_sec = monotonic() - tick
            info = os.fstat(target.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != reader.size:
                raise ValueError("invalid export staging file")
    transport = stream.diagnostics() if prefetch else {
        "enabled": False, "transport": "docker-sdk-export", "chunks": reader.stats.snapshot(),
    }
    transport["consumer_first_byte_wait_sec"] = reader.first_byte_wait_sec
    result = ExportInventory(
        logical, positions, reader.digest.hexdigest() if reader.digest is not None else None,
        reader.size, _identity(info),
        {"receive_sec": reader.receive_sec, "write_sec": reader.write_sec,
         "archive_hash_sec": reader.hash_sec, "sync_sec": sync_sec,
         "parse_files_hash_sec": max(0.0, monotonic() - started - reader.receive_sec
                                     - reader.write_sec - reader.hash_sec - sync_sec)},
        transport,
    )
    result.check_path(path)
    return result


def archive_path(name: str) -> str:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\x00" in name:
        raise ValueError(f"unsafe archive path: {name!r}")
    # Docker imports these as layer whiteouts, not ordinary persistent files.
    if any(part.startswith(".wh.") for part in path.parts):
        raise ValueError(f"Docker whiteout paths are unsupported: {name!r}")
    return str(path)


def excluded(name: str) -> bool:
    return name in SCOPE["exclude_paths"] or any(
        name == root or name.startswith(root + "/") for root in SCOPE["exclude_subtrees"]
    )


def _check_ancestors(kinds):
    """Check a COMPLETE mapping of already canonical relative paths to types.

    Cache only within this immutable inventory. An absent parent is legal, but
    may be cached only after all entries (including late parents) are known.
    Keep root '.' handling identical to PurePosixPath(name).parents.
    """
    checked = set()
    for name in kinds:
        parent = name
        while parent != ".":
            parent = parent.rpartition("/")[0] or "."
            if parent in checked:
                break
            if parent in kinds and kinds[parent] != "directory":
                raise ValueError(f"entry has a non-directory ancestor: {name!r}")
            checked.add(parent)


@timed("rootfs.manifest")
def manifest(path: Path) -> dict:
    """Hash all declared content and supported attributes from one complete plain tar."""
    return _scan(path, locations=False)[0]


@timed("rootfs.inventory")
def inventory(path: Path) -> tuple[dict, dict]:
    """The same logical manifest, plus verified body offsets in a plain tar.

    Offsets are material locations, never part of logical file identity. Equal
    content may share one location without turning independent files into links.
    """
    return _scan(path, locations=True)


def _scan(path, *, locations):
    if path.stat().st_size < 1024 or path.stat().st_size % 512:
        raise ValueError("truncated tar archive")
    with path.open("rb") as source, tarfile.open(fileobj=CountingReader(source), mode="r|") as archive:
        result = _inventory(archive, locations=locations)
        end = archive.offset
    with path.open("rb") as source:
        content = CountingReader(source)
        content.seek(end)
        if content.read(1024) != bytes(1024):
            raise ValueError("tar archive has no complete end marker")
        if any(chunk.strip(b"\0") for chunk in iter(lambda: content.read(1024 * 1024), b"")):
            raise ValueError("unexpected data after tar end marker")
    return result


def _inventory(archive, *, locations):
    entries, links, all_types, bodies = {}, {}, {}, {}
    for member in archive:
        name = archive_path(member.name)
        if name in all_types:
            raise ValueError(f"duplicate archive path: {name!r}")
        all_types[name] = "directory" if member.isdir() else "other"
        if name == "." and not member.isdir():
            raise ValueError("archive root must be a directory")
        if excluded(name):
            continue
        entry = {"mode": member.mode & 0o7777, "uid": member.uid, "gid": member.gid}
        if min(member.uid, member.gid, member.size) < 0:
            raise ValueError("negative owner or file size")
        if member.isdir():
            entry["type"] = "directory"
        elif member.issym():
            if "\x00" in member.linkname:
                raise ValueError("invalid symlink target")
            entry.update(type="symlink", target=member.linkname)
        elif member.islnk():
            entry["type"] = "hardlink"
            links[name] = archive_path(member.linkname)
        elif member.isfile():
            if member.sparse is not None:
                raise ValueError("sparse tar encoding is unsupported")
            digest, size = hashlib.sha256(), 0
            with archive.extractfile(member) as content:
                for chunk in iter(lambda: content.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
                    count(hashed_bytes=len(chunk), manifest_content_bytes=len(chunk))
            if size != member.size:
                raise ValueError("incomplete file in tar")
            entry.update(
                type="file", size=size, sha256=digest.hexdigest(), mtime_sec=int(member.mtime)
            )
            if locations:
                bodies.setdefault(f"{entry['sha256']}:{size}", member.offset_data)
        else:
            raise ValueError(f"unsupported file type in managed scope: {name!r}")
        entries[name] = entry
    _check_ancestors(all_types)
    groups = {}
    for name in links:
        target, seen = name, set()
        while target in links:
            if target in seen:
                raise ValueError("cyclic hardlink")
            seen.add(target)
            target = links[target]
        if target not in entries or entries[target]["type"] != "file":
            raise ValueError("hardlink target missing or outside the managed scope")
        entries[name] = dict(entries[target])
        groups.setdefault(target, {target}).add(name)
    return {
        "schema_version": MANIFEST_VERSION,
        "scope": SCOPE,
        "entries": entries,
        "hardlink_groups": sorted(sorted(group) for group in groups.values()),
    }, bodies


def compare(expected: dict, actual: dict) -> dict:
    left, right = expected["entries"], actual["entries"]
    changes = [p for p in sorted(left.keys() | right.keys()) if left.get(p) != right.get(p)]
    return {
        "equal": expected == actual,
        "different_path_count": len(changes),
        "different_paths": changes[:30],
        "hardlinks_equal": expected["hardlink_groups"] == actual["hardlink_groups"],
    }


@timed("rootfs.export")
def export(container, path: Path) -> None:
    """Stream to private scratch storage. A partial download is never a usable state."""
    stream = container.export(chunk_size=1024 * 1024)
    primary = None
    try:
        with path.open("xb") as target:
            os.chmod(path, 0o600)
            size = 0
            for chunk in stream:
                target.write(chunk)
                size += len(chunk)
                count(written_bytes=len(chunk), exported_bytes=len(chunk))
                temporary_size(path, size)
            with phase("rootfs.sync_file"):
                target.flush()
                os.fsync(target.fileno())
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            if hasattr(stream, "close"):
                stream.close()
        except BaseException as error:
            if primary is None:
                raise
            primary.add_note(f"export stream cleanup also failed: {error!r}")
