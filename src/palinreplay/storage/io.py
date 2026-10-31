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


def _read_metadata(path):
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
        raise IntegrityError("missing or unreadable frozen C0 metadata") from error
