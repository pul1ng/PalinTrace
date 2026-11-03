"""Private output helpers. Reports and exports never overwrite an existing run."""

import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from palinreplay.core.errors import IntegrityError, RecordUsageError
from palinreplay.core.serialization import json_bytes
from palinreplay.storage.artifacts import sync_directory


def write_json(path: Path, value, *, replace: bool = False) -> None:
    fd, name = tempfile.mkstemp(prefix=".report-", dir=path.parent)
    stage = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(stage, path)
        else:
            os.link(stage, path)
        sync_directory(path.parent)
    finally:
        stage.unlink(missing_ok=True)


def outside_store(source: Path, destination: Path) -> None:
    if destination.resolve().is_relative_to(source.resolve()):
        raise RecordUsageError("output must be outside the source Record store")


@contextmanager
def export_directory(destination: Path):
    """Reserve the name, build privately, then replace our empty reservation.

    No valid export appears until all its files have been written. A concurrent
    exporter sees an existing output, and failures remove only our reservation.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(mode=0o700, exist_ok=False)
    reservation = destination.stat()
    published = False
    try:
        with tempfile.TemporaryDirectory(prefix=".record-export-", dir=destination.parent) as tmp:
            stage = Path(tmp) / "export"
            stage.mkdir(mode=0o700)
            yield stage
            current = destination.lstat()
            if (current.st_dev, current.st_ino) != (reservation.st_dev, reservation.st_ino):
                raise FileExistsError("export output changed during publication")
            for directory in sorted(
                (p for p in stage.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts),
                reverse=True,
            ):
                sync_directory(directory)
            sync_directory(stage)
            stage.rename(destination)
            published = True
            sync_directory(destination.parent)
    finally:
        if not published:
            try:
                current = destination.lstat()
                if (current.st_dev, current.st_ino) == (reservation.st_dev, reservation.st_ino):
                    destination.rmdir()
            except (FileNotFoundError, OSError):
                pass


def publish_receipt_json(path, value):
    """Publish once; retract our bytes if directory durability is unconfirmed.

    Visibility alone is insufficient. On failure, reconcile the exact destination
    before removing it. A surviving file after failed retraction is explicitly
    reported as published/unconfirmed to the caller.
    """
    data = json_bytes(value)
    stage = None
    published = synced = False
    identity = None
    try:
        fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
        stage = Path(name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        info = stage.stat()
        identity = (info.st_dev, info.st_ino)
        os.link(stage, path)
        published = True
        stage.unlink()
        stage = None
        sync_directory(path.parent)
        synced = True
        return {"status": "confirmed", "published": True, "directory_synced": True}
    except BaseException as error:
        errors = [f"{type(error).__name__}: {error}"]
        try:
            published = _read_metadata(path, missing_ok=True) == data
            current = path.lstat() if published else None
            ours = current is not None and (current.st_dev, current.st_ino) == identity
            if published and ours and not synced:
                path.unlink()
                published = False
                sync_directory(path.parent)
        except BaseException as diagnostic:
            errors.append(f"retraction: {type(diagnostic).__name__}: {diagnostic}")
        return {"status": "unconfirmed", "published": published,
                "directory_synced": synced, "errors": errors}
    finally:
        if stage is not None:
            try:
                stage.unlink(missing_ok=True)
            except OSError:
                pass  # The publication result already describes this failure.


def _read_metadata(path, *, missing_ok=False):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024**2:
                raise IntegrityError("invalid frozen C0 metadata file")
            source = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with source:
            data = source.read(2 * 1024**2 + 1)
            if len(data) > 2 * 1024**2:
                raise IntegrityError("oversized frozen C0 metadata")
            return data
    except OSError as error:
        if missing_ok and isinstance(error, FileNotFoundError):
            return None
        raise IntegrityError("missing or unreadable frozen C0 metadata") from error
