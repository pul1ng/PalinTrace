"""Versioned task format and package loading, owned by PalinReplay."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from palinreplay.core.schema import Spec

TASK_SCHEMA = "palinreplay.task.v1"
DATASET_SCHEMA = "palinreplay.dataset.v1"


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_repo_path(value: str) -> str:
    """Reject absolute paths, traversal, backslashes and control characters."""
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("path must be a safe relative POSIX path")
    return value


def validate_task_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError("task_id must be a portable directory name without slashes")
    return value


def safe_path(root: Path, relative: str) -> Path:
    validate_repo_path(relative)
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("artifact path resolves outside the task package")
    return resolved


class Artifact(Spec):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    _validate_path = field_validator("path")(validate_repo_path)

    def verify(self, root: Path) -> Path:
        path = safe_path(root, self.path)
        if not path.is_file() or file_sha256(path) != self.sha256:
            raise ValueError(f"missing or corrupt artifact: {self.path}")
        return path


class DatasetSource(Spec):
    """Provenance of converted rows; adapter-specific identifiers stay here."""

    source: str = Field(min_length=1)
    dataset: str = Field(min_length=1)
    split: str = Field(default="test", min_length=1)
    revision: str | None = None
    input_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    fingerprint: str | None = None


class ImageSpec(Spec):
    reference: str
    platform: Literal["linux/amd64", "linux/arm64"]
    source: str
    # This is explicitly a request, NOT evidence of an available or pulled image.
    status: Literal["unresolved"] = "unresolved"

    @field_validator("reference")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*", value) or "://" in value:
            raise ValueError("image reference must be a Docker image name, not a URL or option")
        return value


class EnvironmentSpec(Spec):
    """Provider-neutral execution requirements; adapters supply their own values."""

    backend: str = "docker"
    image: ImageSpec
    workdir: str | None = None
    shell: list[str] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    timeout_sec: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class RepositorySpec(Spec):
    name: str = Field(min_length=1)
    base_commit: str = Field(min_length=1)
    version: str | None = None


class EvaluationSpec(Spec):
    """Evaluation-only material; never part of TaskPackage.agent_input()."""

    kind: str = Field(min_length=1)
    artifacts: list[Artifact] = Field(min_length=1)


class TaskSpec(Spec):
    schema_version: Literal["palinreplay.task.v1"] = TASK_SCHEMA
    task_id: str
    source: DatasetSource
    instruction: Artifact
    environment: EnvironmentSpec
    repository: RepositorySpec | None = None
    evaluation: EvaluationSpec

    _validate_task_id = field_validator("task_id")(validate_task_id)


class TaskEntry(Spec):
    task_id: str
    manifest: Artifact

    _validate_task_id = field_validator("task_id")(validate_task_id)


class DatasetManifest(Spec):
    schema_version: Literal["palinreplay.dataset.v1"] = DATASET_SCHEMA
    source: DatasetSource
    tasks: list[TaskEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_tasks(self) -> "DatasetManifest":
        ids = [entry.task_id for entry in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate task IDs in dataset manifest")
        return self


@dataclass(frozen=True)
class TaskPackage:
    root: Path
    spec: TaskSpec

    @property
    def instruction(self) -> str:
        # read_text() normalizes CRLF/CR; preserve the original problem statement.
        return self.spec.instruction.verify(self.root).read_bytes().decode("utf-8")

    def evaluation_artifacts(self) -> dict[str, Any]:
        """Parse JSON evaluation artifacts, keyed by path, for verifiers only."""
        documents = {}
        for artifact in self.spec.evaluation.artifacts:
            text = artifact.verify(self.root).read_text(encoding="utf-8")
            documents[artifact.path] = json.loads(text) if artifact.path.endswith(".json") else text
        return documents

    def agent_input(self) -> dict[str, Any]:
        """Intentionally excludes gold patches, hidden tests and full source rows."""
        return {
            "task_id": self.spec.task_id,
            "instruction": self.instruction,
            "environment": self.spec.environment.model_dump(mode="json"),
        }


def load_task(path: str | Path) -> TaskPackage:
    path = Path(path)
    manifest_path = path / "task.json" if path.is_dir() else path
    spec = TaskSpec.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.parent.resolve()
    spec.instruction.verify(root)
    for artifact in spec.evaluation.artifacts:
        artifact.verify(root)
    return TaskPackage(root=root, spec=spec)


def load_dataset(path: str | Path) -> tuple[DatasetManifest, list[TaskPackage]]:
    root = Path(path).resolve()
    manifest = DatasetManifest.model_validate_json((root / "manifest.json").read_text("utf-8"))
    tasks = []
    for entry in manifest.tasks:
        task = load_task(entry.manifest.verify(root))
        if task.spec.task_id != entry.task_id:
            raise ValueError(f"task identity does not match dataset manifest: {entry.task_id}")
        if task.spec.source.source != manifest.source.source:
            raise ValueError(
                f"task source adapter does not match dataset manifest: {entry.task_id}"
            )
        tasks.append(task)
    return manifest, tasks
