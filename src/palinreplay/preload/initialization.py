"""Start an acquired preload container and reach an explicit C0 contract.

Usage inside create_preload_environment(inputs)::

    plan = InitializationSpec(
        commands=("python setup_environment.py",),
        checks=("test -f /testbed/ready", "python -c 'import required_package'"),
    )
    result = initialize_preload_environment(environment, inputs, plan)

Instruction remains task text. Each command runs in a new input-configured shell,
as in the existing Sandbox; persistent environment changes belong in inputs.env
or BASH_ENV, and file changes persist in the container. Preparation must finish
synchronously: background services and atomic filesystem freezing are outside
this MVP. Success leaves the container running for the later Btrfs freeze step.
Failures discard the owned container even if the caller catches the exception.
Command/output details are returned in memory, never logged or written here.
"""

import hashlib
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field, field_validator

from palinreplay.core.schema import Spec
from palinreplay.core.serialization import json_bytes
from palinreplay.preload.environment import (
    OWNER_LABEL,
    PreloadCleanupError,
    PreloadEnvironment,
    _check_container,
    _remove_owned,
)
from palinreplay.preload.input import PreloadInput
from palinreplay.sandbox.base import (
    EnvironmentMismatch,
    ExecResult,
    SandboxError,
    SandboxExecutionError,
    SandboxTimeout,
)


class InitializationSpec(Spec):
    """Ordered preparation and required C0 checks; each must exit with zero.

    timeout_sec bounds the entire stage. PreloadInput.timeout_sec additionally
    bounds each Docker operation/command; cleanup has its own bounded attempt.
    """

    schema_version: Literal["palinreplay.preload-initialization.v1"] = (
        "palinreplay.preload-initialization.v1"
    )
    commands: tuple[str, ...] = ()
    checks: tuple[str, ...] = Field(min_length=1)
    timeout_sec: float = Field(default=600.0, gt=0, allow_inf_nan=False)

    @field_validator("commands", "checks")
    @classmethod
    def validate_commands(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not command.strip() or "\x00" in command for command in value):
            raise ValueError("initialization commands must be nonempty strings without NUL")
        return value

    @field_validator("timeout_sec", mode="before")
    @classmethod
    def validate_timeout(cls, value):
        if isinstance(value, bool):
            raise ValueError("timeout_sec must be a positive finite number")
        return value


@dataclass(frozen=True)
class InitializationResult:
    container_id: str
    image_id: str
    input_sha256: str
    initialization_sha256: str
    duration_sec: float
    commands: tuple[ExecResult, ...] = field(repr=False)
    checks: tuple[ExecResult, ...] = field(repr=False)
    status: Literal["ready"] = field(default="ready", init=False)


class PreloadInitializationError(SandboxError):
    def __init__(self, phase: str, index: int, result: ExecResult):
        self.phase, self.index, self.result = phase, index, result
        super().__init__(f"preload {phase} command {index} failed (exit {result.exit_code})")


def _call(function, deadline, timeout, phase):
    budget = min(deadline - time.monotonic(), timeout)
    if budget <= 0:
        raise SandboxTimeout(f"preload initialization deadline exhausted before {phase}")
    results = queue.Queue(maxsize=1)

    def execute():
        try:
            results.put((function(), None))
        except BaseException as error:
            results.put((None, error))

    threading.Thread(target=execute, daemon=True, name="palinreplay-preload-init").start()
    try:
        result, error = results.get(timeout=budget)
    except queue.Empty:
        raise SandboxTimeout(f"preload {phase} exceeded its deadline; no automatic retry") from None
    if error is not None:
        if isinstance(error, (SandboxError, KeyboardInterrupt, SystemExit)):
            raise error
        raise SandboxExecutionError(f"preload {phase} failed; Docker operation outcome unknown") from error
    return result


def _running(container, environment, owner):
    container.reload()
    attrs, config = container.attrs, container.attrs.get("Config") or {}
    if attrs.get("State", {}).get("Status") != "running":
        raise EnvironmentMismatch("preload container is not running")
    if (attrs.get("Image") != environment.image_id
            or config.get("Image") != environment.image_id
            or (config.get("Labels") or {}).get(OWNER_LABEL) != owner):
        raise EnvironmentMismatch("preload container identity changed during initialization")
    if attrs.get("Mounts") or config.get("Volumes"):
        raise EnvironmentMismatch("unexpected mounts during preload initialization")


def initialize_preload_environment(
    environment: PreloadEnvironment, inputs: PreloadInput, plan: InitializationSpec,
) -> InitializationResult:
    """Start once, prepare once, and return ready only after all checks pass.

    The enclosing create_preload_environment context still owns normal cleanup.
    Initialization errors/timeouts remove the container immediately, including
    potentially still-running execs. No partially prepared environment is reused.
    """
    if not isinstance(environment, PreloadEnvironment):
        raise TypeError("environment must be a PreloadEnvironment")
    if not isinstance(inputs, PreloadInput) or not isinstance(plan, InitializationSpec):
        raise TypeError("inputs and plan must be PreloadInput and InitializationSpec")
    inputs = PreloadInput.model_validate(inputs.model_dump())
    plan = InitializationSpec.model_validate(plan.model_dump())
    owner_match = re.fullmatch(r"palinreplay-preload-([0-9a-f]{32})", environment.name)
    if (owner_match is None or environment.container.id != environment.container_id
            or environment.platform != inputs.platform
            or ("@" not in inputs.image and environment.image_id != inputs.image)):
        raise EnvironmentMismatch("preload environment does not match the input")
    owner = owner_match[1]
    container = environment.container
    started = time.monotonic()
    deadline = started + plan.timeout_sec
    verified_owned = False
    commands, checks = [], []

    def call(function, phase):
        return _call(function, deadline, inputs.timeout_sec, phase)

    def execute(command, phase, index):
        tick = time.monotonic()
        result = call(lambda: container.exec_run(
            [*inputs.shell, command], workdir=inputs.workdir, environment=dict(inputs.env),
            stdout=True, stderr=True, stdin=False, tty=False, privileged=False,
            stream=False, demux=False,
        ), phase)
        if (type(getattr(result, "exit_code", None)) is not int
                or not isinstance(getattr(result, "output", None), bytes)):
            raise SandboxExecutionError("preload exec returned no definitive exit code/output")
        result = ExecResult(command, result.output, result.exit_code, time.monotonic() - tick)
        if result.exit_code != 0:
            raise PreloadInitializationError(phase, index, result)
        return result

    def check_workdir():
        result = execute("pwd -P", "workdir", 0)
        if result.text.strip() != inputs.workdir:
            raise EnvironmentMismatch("effective initialization workdir does not match input")
        checks.append(result)
        if "BASH_ENV" in inputs.env:
            checks.append(execute('test -r "$BASH_ENV"', "bash-env", 0))

    try:
        call(lambda: _check_container(container, environment.image_id, inputs, owner), "preflight")
        verified_owned = True
        if (container.attrs.get("Config", {}).get("Labels") or {}).get(
            "palinreplay.preload.task"
        ) != inputs.task_id:
            raise EnvironmentMismatch("preload container task does not match input")
        if "@" in inputs.image:
            image = call(lambda: container.client.images.get(inputs.image), "image-binding")
            if image.id != environment.image_id:
                raise EnvironmentMismatch("preload registry digest resolves to a different image")
        call(container.start, "start")
        call(lambda: _running(container, environment, owner), "startup-check")
        check_workdir()
        for index, command in enumerate(plan.commands, 1):
            commands.append(execute(command, "prepare", index))
        check_workdir()
        for index, command in enumerate(plan.checks, 1):
            checks.append(execute(command, "check", index))
        call(lambda: _running(container, environment, owner), "final-check")
        return InitializationResult(
            container_id=environment.container_id, image_id=environment.image_id,
            input_sha256=hashlib.sha256(json_bytes(inputs.model_dump(mode="json"))).hexdigest(),
            initialization_sha256=hashlib.sha256(json_bytes(plan.model_dump(mode="json"))).hexdigest(),
            duration_sec=time.monotonic() - started,
            commands=tuple(commands), checks=tuple(checks),
        )
    except BaseException as error:
        if verified_owned:
            try:
                _call(
                    lambda: _remove_owned(container.client, container, environment.name, owner),
                    time.monotonic() + 30, 30, "cleanup",
                )
            except Exception as cleanup_error:
                failure = PreloadCleanupError(environment.name, environment.container_id)
                failure.add_note(f"initialization cleanup failed: {type(cleanup_error).__name__}")
                raise failure from error
        raise
