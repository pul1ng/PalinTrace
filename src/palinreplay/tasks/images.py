"""Explicit Docker image preparation. Does not create or start containers."""

import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field

from palinreplay.core.schema import Spec
from palinreplay.tasks.model import file_sha256, json_text, load_dataset


class PreparedImage(Spec):
    reference: str
    platform: str
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    repo_digests: list[str]
    task_ids: list[str]
    archive_path: str | None = None
    archive_sha256: str | None = None


class PreparedImages(Spec):
    schema_version: Literal["palinreplay.images.v1"] = "palinreplay.images.v1"
    dataset_manifest_sha256: str
    images: list[PreparedImage]


def _docker(arguments: list[str], timeout: float, *, stream_output: bool = False) -> str:
    # Preserve Docker's terminal/layer progress while keeping CLI stdout unchanged.
    output = {"stdout": sys.stderr, "stderr": sys.stderr} if stream_output else {}
    try:
        result = subprocess.run(
            ["docker", *arguments], check=True, capture_output=not stream_output,
            text=True, timeout=timeout, **output,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Docker CLI is required for image preparation") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else f"exit code {exc.returncode}"
        raise RuntimeError(f"Docker image operation failed: {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Docker image operation timed out; preparation was not completed"
        ) from exc
    return result.stdout or ""


def _progress(completed: int, total: int, phase: str, reference: str) -> None:
    filled = 24 * completed // total
    bar = "=" * filled + "-" * (24 - filled)
    print(
        f"镜像就绪 [{bar}] {completed}/{total} | {phase} | {reference}",
        file=sys.stderr, flush=True,
    )


def prepare_images(
    dataset_dir: str | Path, *, archive_dir: str | Path | None = None, timeout: float = 900,
    show_progress: bool = False,
) -> PreparedImages:
    """Pull by requested platform, inspect actual identity, optionally export image bytes.

    Writes a separate images.json, never changes task.json or its checksums. An
    unresolved task reference is not evidence that a registry publishes that image.
    All Docker calls happen only when this function is explicitly invoked.
    show_progress writes phase/count progress and Docker's native layer download
    bars to stderr. Library callers remain quiet unless explicitly enabled.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    root = Path(dataset_dir).resolve()
    result_path = root / "images.json"
    if result_path.exists():
        raise FileExistsError(f"image preparation manifest already exists: {result_path}")
    _, tasks = load_dataset(root)
    manifest_digest = file_sha256(root / "manifest.json")
    grouped: dict[tuple[str, str], list[str]] = {}
    for task in tasks:
        if task.spec.environment.backend != "docker":
            raise ValueError("image preparation currently supports Docker tasks only")
        image = task.spec.environment.image
        grouped.setdefault((image.reference, image.platform), []).append(task.spec.task_id)
    archives = Path(archive_dir).resolve() if archive_dir is not None else None
    if archives is not None:
        archives.mkdir(parents=True, exist_ok=True)
    prepared = []
    total = len(grouped)
    for index, ((reference, platform), task_ids) in enumerate(grouped.items(), start=1):
        if show_progress:
            _progress(index - 1, total, f"下载镜像 {index}/{total}", reference)
        _docker(["pull", "--platform", platform, reference], timeout, stream_output=show_progress)
        if show_progress:
            _progress(index - 1, total, "核验镜像", reference)
        response = json.loads(_docker(["image", "inspect", reference], timeout))
        if not isinstance(response, list) or len(response) != 1:
            raise ValueError("Docker inspect did not return exactly one image")
        actual = response[0]
        actual_platform = f"{actual.get('Os')}/{actual.get('Architecture')}"
        if actual_platform != platform:
            raise ValueError(
                f"image platform mismatch: requested {platform}, got {actual_platform}"
            )
        image = PreparedImage(
            reference=reference,
            platform=platform,
            image_id=actual["Id"],
            repo_digests=actual.get("RepoDigests") or [],
            task_ids=task_ids,
        )
        if archives is not None:
            if show_progress:
                _progress(index - 1, total, "归档镜像", reference)
            archive = archives / f"{image.image_id.removeprefix('sha256:')}.tar"
            if archive.exists():
                # Multiple tags can identify one image within this preparation.
                prior = next((item for item in prepared if item.image_id == image.image_id), None)
                if prior is None or prior.archive_path != str(archive):
                    raise FileExistsError(f"image archive already exists: {archive}")
                checksum = prior.archive_sha256
            else:
                with tempfile.TemporaryDirectory(prefix=".image-", dir=archives) as temporary:
                    staged = Path(temporary) / "image.tar"
                    # Save the inspected immutable local ID, not a mutable tag.
                    _docker(["image", "save", "--output", str(staged), image.image_id], timeout)
                    checksum = file_sha256(staged)
                    staged.rename(archive)
            image = image.model_copy(
                update={
                    "archive_path": str(archive),
                    "archive_sha256": checksum,
                }
            )
        prepared.append(image)
        if show_progress:
            _progress(index, total, "镜像已就绪", reference)
    result = PreparedImages(dataset_manifest_sha256=manifest_digest, images=prepared)
    with tempfile.TemporaryDirectory(prefix=".images-", dir=root) as temporary:
        path = Path(temporary) / "images.json"
        path.write_text(json_text(result.model_dump(mode="json")), encoding="utf-8")
        if result_path.exists():
            raise FileExistsError(f"image preparation manifest already exists: {result_path}")
        path.rename(result_path)
    return result
