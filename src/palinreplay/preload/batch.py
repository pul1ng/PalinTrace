"""Select inputs for independent preload builds."""

from collections.abc import Sequence
from pathlib import Path

from palinreplay.preload.build import _load_source, build_preload
from palinreplay.preload.environment import public_keys
from palinreplay.preload.input import PreloadInput
from palinreplay.storage.io import write_json
from palinreplay.tasks.model import load_dataset


def select_preload_inputs(source: str | Path, task_ids: Sequence[str] = ()) -> list[PreloadInput]:
    """Load selected inputs in manifest or requested order before building anything."""
    source = Path(source)
    task_ids = list(task_ids)
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("duplicate task IDs are not allowed")
    if not source.is_dir():
        if len(task_ids) > 1:
            raise ValueError("standalone preload input accepts at most one task ID")
        return [_load_source(source, task_ids[0] if task_ids else None)]

    manifest, _ = load_dataset(source)
    available = [entry.task_id for entry in manifest.tasks]
    if any(task_id not in available for task_id in task_ids):
        raise ValueError("unknown task ID in selection")
    return [_load_source(source, task_id) for task_id in (task_ids or available)]


def build_preloads(source, inputs, output, paths, *, initialization, public_env):
    """Build each preload in order and publish a progressively updated index."""
    output = Path(output).absolute()
    paths = [Path(path).absolute() for path in paths]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    index_path = output / "preloads.json"
    index = {
        "source": str(Path(source).absolute()),
        "status": "running",
        "tasks": [
            {"task_id": item.task_id, "status": "pending", "preload_id": None,
             "path": str(path.relative_to(output)), "duration_sec": None}
            for item, path in zip(inputs, paths, strict=True)
        ],
    }
    results = []
    entry = None
    try:
        write_json(index_path, index)
        for item, path, entry in zip(inputs, paths, index["tasks"], strict=True):
            entry["status"] = "building"
            write_json(index_path, index, replace=True)
            try:
                keys = public_keys(item, public_env)
                result = build_preload(
                    item, path, initialization=initialization, public_env_keys=keys,
                )
            except Exception as error:
                entry["status"] = "failed"
                index["status"] = "failed"
                try:
                    write_json(index_path, index, replace=True)
                except Exception as index_error:
                    error.add_note(f"batch index update also failed ({type(index_error).__name__})")
                raise
            entry.update(
                status="completed", preload_id=result.preload_id,
                path=str(result.path.relative_to(output)), duration_sec=result.duration_sec,
            )
            write_json(index_path, index, replace=True)
            results.append(result)
        index["status"] = "completed"
        write_json(index_path, index, replace=True)
    except KeyboardInterrupt as error:
        if entry is not None and entry["status"] == "building":
            entry["status"] = "cancelled"
        index["status"] = "cancelled"
        try:
            write_json(index_path, index, replace=index_path.exists())
        except Exception as index_error:
            error.add_note(f"batch index update also failed ({type(index_error).__name__})")
        raise
    return results
