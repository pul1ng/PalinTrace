"""Local immutable byte storage used by the Record catalog."""

import hashlib
import io
import os
import stat
import tempfile
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from palinreplay.core.errors import IntegrityError, StoreError
from palinreplay.core.models import ArtifactRef
from palinreplay.diagnostics.metrics import (
    count,
    forget_temporary,
    phase,
    temporary_file,
    temporary_size,
    timed,
)


@timed("artifact.sync_directory")
def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def plain_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise IntegrityError(f"missing directory or unexpected symlink: {path}")


def _file_stamp(value):
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
        value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns,
    )


@dataclass
class _Checks:
    clock_fd: int
    verified: OrderedDict = field(default_factory=OrderedDict)
    clock_reliable: bool = True


@dataclass(frozen=True)
class _StreamReceipt:
    parent: tuple
    stamp: tuple
    expected: ArtifactRef


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root
        self._checks = ContextVar("artifact_checks", default=None)
        self._staged = {}
        self._streamed = {}

    @contextmanager
    def strict_checks(self):
        token = self._checks.set(None)
        try:
            yield
        finally:
            self._checks.reset(token)

    @contextmanager
    def verification_scope(self):
        """Private, single-boundary proofs. Nothing survives the scope or store instance."""
        plain_directory(self.root / ".tmp")
        try:
            clock = tempfile.TemporaryFile(dir=self.root / ".tmp")
        except OSError:
            with self.strict_checks():
                yield
            return
        with clock:
            token = self._checks.set(_Checks(clock.fileno()))
            try:
                yield
            finally:
                self._checks.reset(token)

    @staticmethod
    def _clock_advanced(fd, stamp):
        # Same-filesystem ctime barrier prevents a same-tick write/mtime restore
        # from matching a proof issued before that tick finished. Fall back on
        # coarse timestamps, clock rollback or unsupported probes, never guess.
        deadline = time.monotonic() + 0.05
        try:
            while True:
                os.utime(fd, None)
                clock = os.fstat(fd)
                if clock.st_dev != stamp[0]:
                    return False
                if clock.st_ctime_ns > stamp[-1]:
                    return True
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.001)
        except OSError:
            return False

    def _stamp(self, ref):
        try:
            return _file_stamp(self._path(ref).lstat())
        except OSError as error:
            raise IntegrityError("missing or unreadable artifact") from error

    def _invalidate(self, ref):
        scope = self._checks.get()
        if scope is not None:
            scope.verified.pop((ref.sha256, ref.size), None)

    @timed("artifact.verify_internal")
    def verify_internal(self, ref):
        scope = self._checks.get()
        if scope is None:
            return self.verify(ref)
        key = (ref.sha256, ref.size)
        before = self._stamp(ref)
        if scope.clock_reliable and scope.verified.get(key) == before:
            scope.clock_reliable = self._clock_advanced(scope.clock_fd, before)
            if scope.clock_reliable and self._stamp(ref) == before:
                count(verification_reused=1)
                scope.verified.move_to_end(key)
                return
        if not scope.clock_reliable:
            scope.verified.clear()
        scope.verified.pop(key, None)
        eligible = stat.S_ISREG(before[2]) and not before[2] & 0o222 and before[5] == 1
        reusable = False
        if eligible and scope.clock_reliable:
            scope.clock_reliable = self._clock_advanced(scope.clock_fd, before)
            reusable = scope.clock_reliable
            if not reusable:
                scope.verified.clear()
        scan = self._stamp(ref)
        self.verify(ref)
        after = self._stamp(ref)
        if scan != after:
            raise IntegrityError("artifact changed during verification")
        if reusable and before == scan:
            scope.verified[key] = after
            # Bounded even for adapters that submit a very large material set.
            while len(scope.verified) > 1024:
                scope.verified.popitem(last=False)

    @contextmanager
    def stage_file(self):
        """Give a producer a private, absent path. Close all writers before publication."""
        plain_directory(self.root)
        plain_directory(self.root / ".tmp")
        with tempfile.TemporaryDirectory(prefix="stage-", dir=self.root / ".tmp") as directory:
            parent = Path(directory)
            identity = parent.stat()
            path = parent / "payload"
            self._staged[path] = (identity.st_dev, identity.st_ino)
            with temporary_file(path):
                try:
                    yield path
                finally:
                    self._staged.pop(path, None)
                    self._streamed.pop(path, None)

    @contextmanager
    def _expect_stream(self, path, expected, stamp):
        """Bind an internal stream expectation, NOT a proof of saved bytes.

        Only the owning store's active stage can consume this single-use receipt.
        Publication must still independently hash the final file before returning.
        """
        path = Path(path)
        parent = self._staged.get(path)
        if parent is None or path in self._streamed:
            raise StoreError("stream receipt requires an exclusive active staging path")
        expected = ArtifactRef.model_validate(expected)
        if _file_stamp(path.lstat()) != stamp or stamp[6] != expected.size:
            raise IntegrityError("stream staging identity or size differs")
        self._streamed[path] = _StreamReceipt(parent, stamp, expected)
        try:
            yield
        finally:
            self._streamed.pop(path, None)

    @timed("artifact.publish_staged")
    def publish_staged_file(self, path, media_type):
        """Consume only this store's active staging path; publish without a second copy.

        Ordinary callers retain the staging hash and later reference verification.
        An internal stream receipt instead requires a full final-file verification,
        which may issue the existing single-boundary proof. Transfers are single-use.
        """
        path = Path(path)
        identity = self._staged.pop(path, None)
        receipt = self._streamed.pop(path, None)
        if identity is None:
            raise StoreError("file is not an active staging path owned by this store")
        try:
            parent = path.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode) or (parent.st_dev, parent.st_ino) != identity:
                raise IntegrityError("staging directory was replaced")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as error:
            raise IntegrityError("missing or invalid staging file") from error
        try:
            value = os.fstat(fd)
            if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or value.st_dev != identity[0]:
                raise IntegrityError("staging file must be an exclusive regular file on this disk")
            if receipt is not None and (
                receipt.parent != identity or receipt.stamp != _file_stamp(value)
                or receipt.expected.media_type != media_type
            ):
                raise IntegrityError("staging file differs from the inventoried stream")
            os.fchmod(fd, 0o400)
            source = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with source:
            original = _file_stamp(os.fstat(source.fileno()))
            temporary_size(path, original[6])
            if receipt is None:
                digest, size = hashlib.sha256(), 0
                with phase("artifact.hash_staged"):
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
                        count(read_bytes=len(chunk), hashed_bytes=len(chunk))
                ref = ArtifactRef(sha256=digest.hexdigest(), size=size, media_type=media_type)
            else:
                ref, size = receipt.expected, receipt.expected.size
            with phase("artifact.sync_file"):
                os.fsync(source.fileno())
            if (
                size != original[6] or _file_stamp(os.fstat(source.fileno())) != original
                or _file_stamp(path.lstat()) != original
                or (path.parent.lstat().st_dev, path.parent.lstat().st_ino) != identity
            ):
                raise IntegrityError("staging file changed during publication")
            shard = self.root / ref.sha256[:2]
            try:
                shard.mkdir(mode=0o700)
                sync_directory(self.root)
            except FileExistsError:
                plain_directory(shard)
            destination = self._path(ref)
            try:
                os.link(path, destination, follow_symlinks=False)
            except FileExistsError:
                # Existing but corrupt objects are never silently overwritten.
                if receipt is None:
                    self.verify(ref)
            else:
                published = destination.lstat()
                if (published.st_dev, published.st_ino) != original[:2]:
                    raise IntegrityError("published file differs from the staging inode")
            sync_directory(shard)
        path.unlink()
        forget_temporary(path)
        if receipt is not None:
            # chmod/link/unlink are finished: never carry a pre-publication stamp
            # across their legitimate ctime/nlink changes. Verify final bytes even
            # when an equal object already had a proof earlier in this boundary.
            self._invalidate(ref)
            try:
                before = self._stamp(ref)
                self.verify_internal(ref)
                if self._stamp(ref) != before:
                    raise IntegrityError("published file changed during verification")
            except BaseException:
                self._invalidate(ref)
                # Leave published objects for explicit integrity handling/GC;
                # another writer may already reference them. Only private stage
                # files are ours to clean up. No complete fact is returned here.
                raise
        return ref

    def _path(self, ref: ArtifactRef) -> Path:
        ref = ArtifactRef.model_validate(ref)
        plain_directory(self.root)
        shard = self.root / ref.sha256[:2]
        plain_directory(shard)
        return shard / ref.sha256[2:]

    def _open(self, ref: ArtifactRef):
        try:
            path = self._path(ref)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise IntegrityError(f"artifact is not a regular file: {ref.sha256}")
                return os.fdopen(fd, "rb")
            except BaseException:
                os.close(fd)
                raise
        except OSError as exc:
            raise IntegrityError(f"missing or unreadable artifact {ref.sha256}: {exc}") from exc

    @timed("artifact.verify")
    def verify(self, ref: ArtifactRef) -> None:
        self._invalidate(ref)
        count(verification_calls=1)
        with self._open(ref) as handle:
            digest, size = hashlib.sha256(), 0
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
                count(read_bytes=len(chunk), hashed_bytes=len(chunk), verified_bytes=len(chunk))
        if size != ref.size or digest.hexdigest() != ref.sha256:
            raise IntegrityError(f"artifact size/hash mismatch: {ref.sha256}")

    @timed("artifact.read_verify")
    def read(self, ref: ArtifactRef) -> bytes:
        self._invalidate(ref)
        count(verification_calls=1)
        with self._open(ref) as handle:
            data = handle.read()
        count(read_bytes=len(data), verified_bytes=len(data))
        if len(data) != ref.size:
            raise IntegrityError(f"artifact size/hash mismatch: {ref.sha256}")
        count(hashed_bytes=len(data))
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise IntegrityError(f"artifact size/hash mismatch: {ref.sha256}")
        return data

    @timed("artifact.copy_verify")
    def copy_to(self, ref: ArtifactRef, destination: Path) -> None:
        """Materialize a verified private copy without loading a large object into memory."""
        self._invalidate(ref)
        created = False
        count(verification_calls=1)
        try:
            with self._open(ref) as source, destination.open("xb") as target:
                created = True
                os.fchmod(target.fileno(), 0o600)
                digest, size = hashlib.sha256(), 0
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    count(read_bytes=len(chunk), written_bytes=len(chunk), copied_bytes=len(chunk),
                          hashed_bytes=len(chunk), verified_bytes=len(chunk))
                if size != ref.size or digest.hexdigest() != ref.sha256:
                    raise IntegrityError(f"artifact size/hash mismatch: {ref.sha256}")
                with phase("artifact.sync_file"):
                    target.flush()
                    os.fsync(target.fileno())
            sync_directory(destination.parent)
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise

    @timed("artifact.write")
    def put_stream(self, stream: BinaryIO, media_type: str) -> ArtifactRef:
        plain_directory(self.root)
        temporary = self.root / ".tmp"
        plain_directory(temporary)
        fd, name = tempfile.mkstemp(prefix="put-", dir=temporary)
        stage = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                digest, size = hashlib.sha256(), 0
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    count(written_bytes=len(chunk), hashed_bytes=len(chunk))
                    temporary_size(stage, size)
                with phase("artifact.sync_file"):
                    handle.flush()
                    os.fchmod(handle.fileno(), 0o400)
                    os.fsync(handle.fileno())
            ref = ArtifactRef(sha256=digest.hexdigest(), size=size, media_type=media_type)
            shard = self.root / ref.sha256[:2]
            try:
                shard.mkdir(mode=0o700)
                sync_directory(self.root)
            except FileExistsError:
                plain_directory(shard)
            destination = self._path(ref)
            try:
                # Publish without ever replacing a pre-existing object, even if
                # another Store wrote the same bytes concurrently.
                os.link(stage, destination, follow_symlinks=False)
            except FileExistsError:
                self.verify(ref)
            sync_directory(shard)
            return ref
        finally:
            stage.unlink(missing_ok=True)
            forget_temporary(stage)

    def put(self, data: bytes, media_type: str) -> ArtifactRef:
        if not isinstance(data, bytes):
            raise TypeError("raw artifacts must be bytes")
        return self.put_stream(io.BytesIO(data), media_type)
