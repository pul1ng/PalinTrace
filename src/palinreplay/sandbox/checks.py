"""Read-only startup checks; never clone, checkout or reset the supplied repository."""

import json
import shlex
from pathlib import PurePosixPath

from palinreplay.sandbox.base import (
    INITIALIZATION_POLICY,
    EnvironmentMismatch,
    ExecResult,
    Sandbox,
    StartupChecks,
)
from palinreplay.sandbox.repository import verify_repository
from palinreplay.tasks.model import TaskPackage


def verify_environment(
    sandbox: Sandbox, task: TaskPackage, *, image_head: str | None
) -> StartupChecks:
    results = []

    def execute_check(command: str, *, git: bool = False) -> ExecResult:
        env = {"GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1"} if git else None
        result = sandbox.exec(command, env=env)
        results.append(result)
        if result.exit_code != 0:
            raise EnvironmentMismatch(
                f"startup check failed (exit {result.exit_code}): {command}\n{result.text}"
            )
        return result

    def checked(command: str) -> str:
        return execute_check(command).text.strip()

    expected = task.spec.environment
    workdir = checked("pwd -P")
    if workdir != str(PurePosixPath(expected.workdir or "/")):
        raise EnvironmentMismatch(f"workdir mismatch: expected {expected.workdir}, got {workdir}")
    bash_env = None
    if "BASH_ENV" in expected.env:
        bash_env = checked('test -r "$BASH_ENV" && printf "%s\\n" "$BASH_ENV"')
        if bash_env != expected.env["BASH_ENV"]:
            raise EnvironmentMismatch("BASH_ENV does not match the task")
    conda_env, python_executable = None, None
    if task.spec.source.source in {
        "swebench_full",
        "swebench_lite",
        "swebench_verified",
        "swe-bench",
    }:
        # This requirement belongs to the SWE-bench image contract, not all tasks.
        probe = (
            "import json, os, sys; "
            "print(json.dumps(dict(conda_env=os.getenv('CONDA_DEFAULT_ENV'), "
            "conda_prefix=os.getenv('CONDA_PREFIX'), prefix=sys.prefix, executable=sys.executable)))"
        )
        try:
            data = json.loads(checked(f"python -c {shlex.quote(probe)}"))
            conda_env = data["conda_env"]
            python_executable = data["executable"]
            if (
                conda_env != "testbed"
                or data["prefix"] != data["conda_prefix"]
                or not python_executable
            ):
                raise EnvironmentMismatch("SWE-bench's testbed conda environment is not active")
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise EnvironmentMismatch("invalid SWE-bench environment probe output") from exc
    repository = None
    if task.spec.repository is not None:
        if image_head is None:
            raise EnvironmentMismatch("preserve-image requires a pre-start image Git HEAD")
        repository = verify_repository(
            task.spec.repository.base_commit,
            image_head,
            lambda command: execute_check(command, git=True),
        )
    return StartupChecks(
        initialization=INITIALIZATION_POLICY,
        workdir=workdir,
        repository=repository,
        bash_env=bash_env,
        conda_env=conda_env,
        python_executable=python_executable,
        commands=tuple(results),
    )
