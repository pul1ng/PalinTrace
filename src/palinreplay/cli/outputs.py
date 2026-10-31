"""Default paths for CLI products; library callers still choose their own paths."""

import re
from datetime import datetime
from pathlib import Path

from palinreplay.core.errors import RecordUsageError
from palinreplay.core.models import Run
from palinreplay.storage.store import RecordStore
from palinreplay.tasks.download import FetchRecord
from palinreplay.tasks.model import DatasetManifest


def timestamp() -> str:
    return datetime.now().strftime("%y%m%d_%H%M%S")


def default_output(kind: str, name: str, *, suffix: str = "") -> Path:
    # Inputs may already be generated paths or dataset names containing slashes.
    name = re.sub(r"^(?:\d{6}_\d{6}_)+", "", name)
    name = re.sub(r"[^\w.-]+", "_", name).strip("._-") or "result"
    return Path.cwd() / "outputs" / kind / f"{timestamp()}_{name}{suffix}"


def conversion_name(path: Path, source: str | None) -> str:
    if source is not None:
        return source
    fetch = path / "fetch.json"
    if fetch.is_file():
        return FetchRecord.model_validate_json(fetch.read_text()).source
    return path.stem


def task_name(dataset: Path, task_id: str | None) -> str:
    if task_id is not None:
        return task_id
    manifest = DatasetManifest.model_validate_json((dataset / "manifest.json").read_text())
    if len(manifest.tasks) != 1:
        raise RecordUsageError("select one task with --task-id")
    return manifest.tasks[0].task_id


def record_name(source: Path) -> str:
    with RecordStore.open(source) as store, store.snapshot():
        runs = store.list(Run)
        if len(runs) == 1:
            return runs[0].task_id
    return source.parent.name if source.name == "record" else source.name
