"""Select inputs for independent preload builds."""

from collections.abc import Sequence
from pathlib import Path

from palinreplay.preload.build import _load_source, build_preload
from palinreplay.preload.environment import public_keys
from palinreplay.preload.input import PreloadInput
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


def build_preloads(inputs, output, paths, *, initialization, public_env):
    """Build each selected preload in order using separate single-task lifecycles."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    results = []
    for item, path in zip(inputs, paths, strict=True):
        keys = public_keys(item, public_env)
        results.append(build_preload(
            item, path, initialization=initialization, public_env_keys=keys,
        ))
    return results
