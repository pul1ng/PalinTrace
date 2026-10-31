"""Single-container Docker SDK backend. No pull/build, bind mounts or host shell execution."""

import math
import queue
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from palinreplay.sandbox.base import (
    INITIALIZATION_POLICY,
    EnvironmentMismatch,
    ExecResult,
    SandboxCleanupError,
    SandboxError,
    SandboxExecutionError,
    SandboxInfo,
    SandboxTimeout,
    StartupChecks,
)
from palinreplay.sandbox.binding import SandboxTask, load_sandbox_task
from palinreplay.sandbox.checks import verify_environment
from palinreplay.sandbox.client import connect_docker
from palinreplay.sandbox.repository import read_image_head

OWNER_LABEL = "palinreplay.owner"


def _positive(value: float, name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")


def _workdir(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise ValueError("workdir must be an absolute container path")
    return str(PurePosixPath(value))


def _missing(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 404


class DockerSDKSandbox:
    """One fresh container per object. start/exec/inspect/close are serialized.

    Tool nonzero exits are results. Timeout/transport ambiguity aborts this
    sandbox, with no automatic retry or restart. Use as a context manager.
    """

    def __init__(
        self,
        binding: SandboxTask,
        *,
        client: Any = None,
        run_id: str | None = None,
        network_mode: str = "bridge",
        memory_bytes: int = 2 * 1024**3,
        cpus: float = 2.0,
        api_timeout_sec: float = 15.0,
    ):
        _positive(cpus, "cpus")
        if int(cpus * 1_000_000_000) < 1:
            raise ValueError("cpus must be at least one nanocpu")
        _positive(api_timeout_sec, "api_timeout_sec")
        if not isinstance(memory_bytes, int) or isinstance(memory_bytes, bool) or memory_bytes <= 0:
            raise ValueError("memory_bytes must be a positive integer")
        if network_mode not in {"bridge", "none"}:
            raise ValueError(
                "network_mode must be bridge or none; host networking is not supported"
            )
        self.binding = binding
        self.task = binding.task
        self.image = binding.image
        self._workdir = _workdir(self.task.spec.environment.workdir or "/")
        self._shell = list(self.task.spec.environment.shell or ["bash", "-c"])
        if not self._shell or any(
            not isinstance(s, str) or not s or "\x00" in s for s in self._shell
        ):
            raise ValueError("shell must contain nonempty arguments")
        self._env = dict(self.task.spec.environment.env)
        self._timeout = self.task.spec.environment.timeout_sec or 60.0
        self._client = client
        self._owns_client = client is None
        self._owner = uuid.uuid4().hex
        self.name = f"pr-{self._owner}"
        self.run_id = run_id or self._owner
        self._labels = {
            OWNER_LABEL: self._owner,
            "palinreplay.run": self.run_id,
            "palinreplay.task": self.task.spec.task_id,
        }
        self._network = network_mode
        self._memory = memory_bytes
        self._cpus = int(cpus * 1_000_000_000)
        self._api_timeout = api_timeout_sec
        self._container = None
        self._create_attempted = False
        self._daemon_platform = ""
        self._lock = threading.RLock()
        self.state = "new"
        self.last_container_id: str | None = None
        self.checks: StartupChecks | None = None

    @classmethod
    def from_task_package(cls, dataset_dir: str | Path, task_id: str | None = None, **kwargs):
        return cls(load_sandbox_task(dataset_dir, task_id), **kwargs)

    def start(self) -> SandboxInfo:
        with self._lock:
            if self.state != "new":
                raise SandboxError("Sandbox is single-use; start requires a new object")
            self.state = "starting"
            try:
                if self._client is None:
                    self._client = connect_docker(self._api_timeout)
                daemon = self._client.info()
                arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(
                    daemon.get("Architecture"), daemon.get("Architecture", "unknown")
                )
                self._daemon_platform = f"{daemon.get('OSType')}/{arch}"
                if daemon.get("OSType") != "linux":
                    raise EnvironmentMismatch("Sandbox requires a Linux Docker daemon")
                # .create(), unlike .run(), does not silently pull a missing image.
                local = self._client.images.get(self.image.image_id)
                actual = local.attrs
                if local.id != self.image.image_id:
                    raise EnvironmentMismatch("local image ID does not match images.json")
                if f"{actual.get('Os')}/{actual.get('Architecture')}" != self.image.platform:
                    raise EnvironmentMismatch("local image platform does not match images.json")
                if actual.get("Config", {}).get("Volumes"):
                    raise EnvironmentMismatch(
                        "image-declared volumes are not supported in this release"
                    )
                self._create_attempted = True
                self._container = self._client.containers.create(
                    image=self.image.image_id,
                    entrypoint=["sleep"],
                    command=["infinity"],
                    name=self.name,
                    labels=self._labels,
                    working_dir=self._workdir,
                    environment=self._env,
                    platform=self.image.platform,
                    network_mode=self._network,
                    mem_limit=self._memory,
                    nano_cpus=self._cpus,
                    detach=True,
                    tty=False,
                    stdin_open=False,
                    privileged=False,
                    auto_remove=False,
                    volumes={},
                    ports={},
                )
                self.last_container_id = self._container.id
                # Capture the prepared repository before sleep, BASH_ENV, or
                # any other process can alter it. No extra container or exec.
                image_head = None
                if self.task.spec.repository is not None:
                    image_head = read_image_head(self._container, self._workdir)
                self._container.start()
                info = self.inspect()
                if info.status != "running":
                    raise EnvironmentMismatch(f"container is not running: {info.status}")
                if info.mounts:
                    raise EnvironmentMismatch("unexpected container mounts; refusing to run")
                if info.environment != self._env:
                    raise EnvironmentMismatch("container environment does not match the task")
                self.checks = verify_environment(self, self.task, image_head=image_head)
                self.state = "running"
                return self.inspect()
            except BaseException as exc:
                # A failed startup exec may already have discarded the sandbox
                # and closed our SDK client. Do not reopen cleanup in that case.
                if self.state != "closed":
                    self.state = "failed"
                self._cleanup_after_error(exc)
                if isinstance(exc, (SandboxError, KeyboardInterrupt, SystemExit)):
                    raise
                raise SandboxError(f"sandbox startup failed: {type(exc).__name__}: {exc}") from exc

    def _cleanup_after_error(self, original: BaseException) -> None:
        try:
            self.close()
        except Exception as cleanup:
            raise SandboxCleanupError(
                f"{original}; cleanup unconfirmed for {self.last_container_id or self.name}: {cleanup}"
            ) from original

    def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> ExecResult:
        with self._lock:
            if self.state not in {"starting", "running"} or self._container is None:
                raise SandboxError("Sandbox is not running")
            # An empty shell command is valid (mini's bash schema permits it).
            # Pass it through verbatim, including normal shell initialization.
            if not isinstance(command, str) or "\x00" in command:
                raise ValueError("command must be a string without NUL")
            timeout = self._timeout if timeout_sec is None else timeout_sec
            _positive(timeout, "timeout_sec")
            workdir = self._workdir if cwd is None else _workdir(cwd)
            environment = self._env | (env or {})
            if any(
                not isinstance(k, str)
                or not k
                or "=" in k
                or "\x00" in k
                or not isinstance(v, str)
                or "\x00" in v
                for k, v in environment.items()
            ):
                raise ValueError("environment must contain valid string keys and values")
            results: queue.Queue = queue.Queue(maxsize=1)
            container = self._container

            def execute() -> None:
                try:
                    result = container.exec_run(
                        [*self._shell, command],
                        workdir=workdir,
                        environment=environment,
                        stdout=True,
                        stderr=True,
                        stdin=False,
                        tty=False,
                        privileged=False,
                        stream=False,
                        demux=False,
                    )
                    results.put((result, None))
                except Exception as error:
                    results.put((None, error))

            start = time.monotonic()
            # SDK streaming sockets can disable HTTP read timeouts. This is a
            # command deadline, not a claim that a timed-out exec has stopped.
            worker = threading.Thread(target=execute, daemon=True, name=f"{self.name}-exec")
            worker.start()
            try:
                result, error = results.get(timeout=timeout)
                if error is not None:
                    raise SandboxExecutionError(
                        f"Docker exec failed; command outcome unknown ({type(error).__name__})"
                    ) from error
                if not isinstance(result.exit_code, int) or not isinstance(result.output, bytes):
                    raise SandboxExecutionError(
                        "Docker exec returned no definitive exit code/output"
                    )
                return ExecResult(
                    command, result.output, result.exit_code, time.monotonic() - start
                )
            except queue.Empty:
                error = SandboxTimeout(
                    f"command exceeded {timeout:g}s; Sandbox discarded, no retry"
                )
                self.state = "failed"
                self._cleanup_after_error(error)
                raise error from None
            except BaseException as exc:
                self.state = "failed"
                self._cleanup_after_error(exc)
                raise

    def inspect(self) -> SandboxInfo:
        with self._lock:
            if self._container is None or self.state == "closed":
                raise SandboxError("Sandbox has no live container")
            self._container.reload()
            attrs = self._container.attrs
            config = attrs.get("Config", {})
            labels = config.get("Labels") or {}
            if labels.get(OWNER_LABEL) != self._owner:
                raise EnvironmentMismatch("container ownership label does not match")
            if config.get("Image") != self.image.image_id:
                raise EnvironmentMismatch("container was not created from the pinned image ID")
            environment = dict(item.split("=", 1) for item in config.get("Env", []) if "=" in item)
            host = attrs.get("HostConfig", {})
            return SandboxInfo(
                run_id=self.run_id,
                task_id=self.task.spec.task_id,
                container_id=self._container.id,
                name=attrs.get("Name", "").lstrip("/"),
                image_id=self.image.image_id,
                actual_image_id=attrs.get("Image", ""),
                platform=self.image.platform,
                initialization=INITIALIZATION_POLICY,
                daemon_platform=self._daemon_platform,
                status=attrs.get("State", {}).get("Status", "unknown"),
                workdir=config.get("WorkingDir", ""),
                shell=tuple(self._shell),
                environment={k: environment[k] for k in self._env if k in environment},
                labels=labels,
                network_mode=host.get("NetworkMode", ""),
                memory_bytes=host.get("Memory", 0),
                nano_cpus=host.get("NanoCpus", 0),
                mounts=tuple(m.get("Destination", "") for m in attrs.get("Mounts", [])),
            )

    def close(self) -> None:
        with self._lock:
            if self.state == "closed":
                return
            self.state = "closing"
            try:
                container = self._container
                if container is None and self._create_attempted:
                    # A create request may succeed at the daemon but lose its
                    # response. Recover only our random name AND ownership label.
                    try:
                        container = self._client.containers.get(self.name)
                    except Exception as exc:
                        if not _missing(exc):
                            raise
                if container is not None:
                    self.last_container_id = container.id
                    try:
                        container.reload()
                        if (container.attrs.get("Config", {}).get("Labels") or {}).get(
                            OWNER_LABEL
                        ) != self._owner:
                            raise SandboxCleanupError(
                                "refusing to remove a container we do not own"
                            )
                        container.remove(force=True, v=True)
                    except Exception as exc:
                        if not _missing(exc):
                            raise
                    try:
                        self._client.containers.get(container.id)
                    except Exception as exc:
                        if not _missing(exc):
                            raise
                    else:
                        raise SandboxCleanupError("container still exists after removal")
                self._container = None
                self.state = "closed"
            except Exception as exc:
                self.state = "failed"
                raise SandboxCleanupError(
                    f"cannot confirm removal of {self.last_container_id or self.name}: {exc}"
                ) from exc
            finally:
                if self.state == "closed" and self._owns_client and self._client is not None:
                    self._client.close()
                    self._client = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc is None:
            self.close()
        else:
            self._cleanup_after_error(exc)
        return False
