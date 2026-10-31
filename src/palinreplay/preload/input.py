"""Read preload inputs without resolving images or executing instructions.

``load_preload_input(path)`` reads a standalone JSON document, for example::

    {
      "image": "registry.example/task@sha256:<64 lowercase hex characters>",
      "platform": "linux/amd64",
      "instruction": "Fix the reported bug.",
      "env": {"LANG": "C.UTF-8"},
      "workdir": "/testbed"
    }

``load_preload_task(dataset_dir, task_id=...)`` adapts an existing task package
and its images.json. Prepared identities are input claims, not evidence that a
Docker daemon currently has the image. Registry digests and local image IDs are
different kinds of identity and are not compared as if they were interchangeable.
"""

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, field_validator

from palinreplay.core.schema import Spec
from palinreplay.tasks.model import ImageSpec, validate_task_id

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _repository_digest(value: str) -> str:
    if value.count("@") != 1:
        raise ValueError("repository digest must be a repository@sha256:<64 hex characters>")
    reference, digest = value.split("@")
    ImageSpec.validate_reference(reference)
    if not _SHA256.fullmatch(digest):
        raise ValueError("repository digest requires a full lowercase SHA-256 digest")
    return value


class PreloadInput(Spec):
    """Versioned input configuration; instruction is task text, not a setup script.

    ``image`` is either a complete local image ID or a pullable repository digest.
    ``image_reference`` and ``repo_digests`` preserve optional task provenance.
    Environment values come only from the supplied configuration, never the host.
    """

    schema_version: Literal["palinreplay.preload-input.v1"] = "palinreplay.preload-input.v1"
    image: str
    platform: Literal["linux/amd64", "linux/arm64"]
    instruction: str
    env: dict[str, str] = Field(default_factory=dict)
    workdir: str = "/"
    shell: tuple[str, ...] = ("bash", "-c")
    timeout_sec: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    task_id: str | None = None
    image_reference: str | None = None
    repo_digests: tuple[str, ...] = ()

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        if _SHA256.fullmatch(value):
            return value
        return _repository_digest(value)

    @field_validator("instruction")
    @classmethod
    def validate_instruction(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("instruction must contain task text")
        return value  # Preserve whitespace, Unicode and original newline characters.

    @field_validator("env")
    @classmethod
    def validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        for key, content in value.items():
            if not key or "=" in key or "\x00" in key or "\x00" in content:
                raise ValueError("environment names and values must be valid container strings")
        return value

    @field_validator("workdir")
    @classmethod
    def validate_workdir(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise ValueError("workdir must be an absolute container path without parent traversal")
        return str(path)

    @field_validator("shell")
    @classmethod
    def validate_shell(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not argument or "\x00" in argument for argument in value):
            raise ValueError("shell must contain nonempty arguments without NUL")
        return value

    @field_validator("timeout_sec", mode="before")
    @classmethod
    def validate_timeout(cls, value):
        if isinstance(value, bool):
            raise ValueError("timeout_sec must be a positive finite number")
        return value

    @field_validator("task_id")
    @classmethod
    def validate_task(cls, value: str | None) -> str | None:
        return validate_task_id(value) if value is not None else None

    @field_validator("image_reference")
    @classmethod
    def validate_reference(cls, value: str | None) -> str | None:
        return ImageSpec.validate_reference(value) if value is not None else None

    @field_validator("repo_digests")
    @classmethod
    def validate_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_repository_digest(digest) for digest in value)

    @property
    def instruction_sha256(self) -> str:
        return hashlib.sha256(self.instruction.encode("utf-8")).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field in preload input")
        result[key] = value
    return result


def load_preload_input(path: str | Path) -> PreloadInput:
    """Load and validate standalone JSON; unknown and duplicate fields are errors."""
    data = json.loads(Path(path).read_bytes().decode("utf-8"), object_pairs_hook=_unique_object)
    return PreloadInput.model_validate(data)


def load_preload_task(dataset_dir: str | Path, task_id: str | None = None) -> PreloadInput:
    """Read a prepared project task using the existing package/binding checks.

    Missing images.json is an input error; image preparation is a separate step.
    Evaluation artifacts may be checked by the task loader but are not copied to
    preload inputs. This function never writes the source package.
    """
    from palinreplay.sandbox.binding import load_sandbox_task

    binding = load_sandbox_task(dataset_dir, task_id)
    task = binding.task
    artifact = task.spec.instruction
    content = artifact.verify(task.root).read_bytes()
    if hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise ValueError("instruction changed while reading preload input")
    environment = task.spec.environment
    return PreloadInput(
        task_id=task.spec.task_id,
        image=binding.image.image_id,
        image_reference=binding.image.reference,
        repo_digests=tuple(binding.image.repo_digests),
        platform=binding.image.platform,
        instruction=content.decode("utf-8"),
        env=environment.env,
        workdir=environment.workdir if environment.workdir is not None else "/",
        shell=tuple(environment.shell) if environment.shell is not None else ("bash", "-c"),
        timeout_sec=environment.timeout_sec if environment.timeout_sec is not None else 60.0,
    )
