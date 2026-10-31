"""Small, project-owned runtime contract; no Agent or snapshot dependency."""

from dataclasses import dataclass
from typing import Protocol

INITIALIZATION_POLICY = "preserve-image"


class SandboxError(RuntimeError):
    """Lifecycle or infrastructure failure, not a tool's nonzero exit code."""


class EnvironmentMismatch(SandboxError):
    """The prepared image or running environment does not match the task."""


class SandboxExecutionError(SandboxError):
    """The command outcome is unknown; it must not be silently retried."""


class SandboxTimeout(SandboxExecutionError):
    """Wall-clock deadline exceeded; the sandbox is discarded, not reused."""


class SandboxCleanupError(SandboxError):
    """Removal was not confirmed; keep the container identity for manual recovery."""


@dataclass(frozen=True)
class ExecResult:
    command: str
    output: bytes  # merged stdout/stderr, without TTY processing or decoding loss
    exit_code: int
    duration_sec: float

    @property
    def text(self) -> str:
        """Display view only; output retains the original bytes."""
        return self.output.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class SandboxInfo:
    run_id: str
    task_id: str
    container_id: str
    name: str
    image_id: str  # requested immutable ID from images.json
    actual_image_id: str  # Docker container inspect's Image field
    platform: str
    initialization: str
    daemon_platform: str
    status: str
    workdir: str
    shell: tuple[str, ...]
    environment: dict[str, str]  # actual values of task-declared environment keys
    labels: dict[str, str]
    network_mode: str
    memory_bytes: int
    nano_cpus: int
    mounts: tuple[str, ...]


@dataclass(frozen=True)
class RepositoryChecks:
    base_commit: str  # immutable task provenance, not the expected runtime HEAD
    image_head: str  # read from the created container BEFORE any process starts
    head: str  # observed runtime HEAD; must still equal image_head
    tree: str
    relation: str  # same or descendant (relative to base_commit)
    commits_ahead: int
    diff_sha256: str  # raw Git diff, including modes and object IDs
    changed_files: int
    mode_only_files: int
    content_changed_files: int
    tracked_worktree_clean: bool


@dataclass(frozen=True)
class StartupChecks:
    initialization: str
    workdir: str
    repository: RepositoryChecks | None
    bash_env: str | None
    conda_env: str | None
    python_executable: str | None
    commands: tuple[ExecResult, ...]

    @property
    def base_commit(self) -> str | None:
        """Keep the old accessor, but always return the task's source commit."""
        return self.repository.base_commit if self.repository is not None else None


class Sandbox(Protocol):
    """One lifetime, one writable environment; execs are new shells, not sessions."""

    def start(self) -> SandboxInfo: ...

    def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> ExecResult: ...

    def inspect(self) -> SandboxInfo: ...

    def close(self) -> None: ...
