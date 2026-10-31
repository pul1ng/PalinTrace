"""Validate task artifacts and the independent prepared-image manifest before Docker."""

from dataclasses import dataclass
from pathlib import Path

from palinreplay.tasks.images import PreparedImage, PreparedImages
from palinreplay.tasks.model import TaskPackage, file_sha256, load_dataset


@dataclass(frozen=True)
class SandboxTask:
    task: TaskPackage
    image: PreparedImage
    dataset_manifest_sha256: str
    images_manifest_sha256: str


def load_sandbox_task(dataset_dir: str | Path, task_id: str | None = None) -> SandboxTask:
    root = Path(dataset_dir).resolve()
    manifest, tasks = load_dataset(root)
    images_path = root / "images.json"
    if not images_path.is_file():
        raise ValueError("missing images.json; run pal download mirror on this task package first")
    prepared = PreparedImages.model_validate_json(images_path.read_bytes())
    digest = file_sha256(root / "manifest.json")
    if prepared.dataset_manifest_sha256 != digest:
        raise ValueError("images.json does not match the task package manifest")
    by_id = {task.spec.task_id: task for task in tasks}
    bound = {}
    for image in prepared.images:
        if not image.task_ids:
            raise ValueError("prepared image has no task IDs")
        for key in image.task_ids:
            if key not in by_id or key in bound:
                raise ValueError(f"unknown or duplicate prepared task ID: {key}")
            task = by_id[key]
            if task.spec.source != manifest.source:
                raise ValueError(f"task provenance does not match dataset: {key}")
            expected = task.spec.environment.image
            if (image.reference, image.platform) != (expected.reference, expected.platform):
                raise ValueError(f"prepared image reference/platform does not match task: {key}")
            bound[key] = image
    if bound.keys() != by_id.keys():
        raise ValueError("images.json does not cover every task in the package")
    if task_id is None:
        if len(tasks) != 1:
            raise ValueError("task_id is required for a package containing multiple tasks")
        task_id = tasks[0].spec.task_id
    if task_id not in by_id:
        raise ValueError(f"unknown task ID: {task_id}")
    task = by_id[task_id]
    if task.spec.environment.backend != "docker":
        raise ValueError("DockerSDKSandbox supports Docker tasks only")
    return SandboxTask(task, bound[task_id], digest, file_sha256(images_path))
