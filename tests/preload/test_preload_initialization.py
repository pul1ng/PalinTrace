import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from palinreplay.preload.environment import PreloadCleanupError, create_preload_environment
from palinreplay.preload.initialization import (
    InitializationSpec,
    PreloadInitializationError,
    initialize_preload_environment,
)
from palinreplay.sandbox.base import EnvironmentMismatch, SandboxExecutionError, SandboxTimeout
from tests.preload.test_preload_environment import DIGEST, OTHER_ID, Client, inputs


def setup_runtime(client):
    def configure(container):
        container.calls = []
        container.starts = 0
        container.responses = {}
        container.exec_entered, container.exec_stopped = threading.Event(), threading.Event()
        container.start_failure = None
        container.start_status = "running"
        container.after_exec = lambda command: None
        original_remove = container.remove

        def start():
            container.starts += 1
            if container.start_failure is not None:
                raise container.start_failure
            container.attrs["State"]["Status"] = container.start_status

        def execute(argv, **kwargs):
            command = argv[-1]
            container.calls.append((argv, kwargs))
            if command == "hang":
                container.exec_entered.set()
                assert container.exec_stopped.wait(5), "cleanup must stop the exec"
                return SimpleNamespace(exit_code=137, output=b"stopped")
            result = container.responses.get(command)
            if isinstance(result, BaseException):
                raise result
            if result is None:
                output = b"/testbed\n" if command == "pwd -P" else b"ok\n"
                result = SimpleNamespace(exit_code=0, output=output)
            container.after_exec(command)
            return result

        def remove(**kwargs):
            original_remove(**kwargs)
            if container.removed:
                container.exec_stopped.set()

        container.start, container.exec_run, container.remove = start, execute, remove

    client.on_create = configure


def plan(**kwargs):
    return InitializationSpec(**({"commands": ("prepare-a", "prepare-b"), "checks": ("ready",)} | kwargs))


def test_start_prepare_and_check_reach_c0_in_order_without_executing_instruction():
    client, spec = Client(), inputs()
    setup_runtime(client)
    with create_preload_environment(spec, client=client) as environment:
        result = initialize_preload_environment(environment, spec, plan())
        assert result.status == "ready" and result.container_id == environment.container_id
        assert result.image_id == environment.image_id
        assert len(result.input_sha256) == len(result.initialization_sha256) == 64
        assert [value.command for value in result.commands] == ["prepare-a", "prepare-b"]
        container = environment.container
        assert container.starts == 1 and not container.removed
        assert [argv[-1] for argv, _ in container.calls] == [
            "pwd -P", "prepare-a", "prepare-b", "pwd -P", "ready",
        ]
        for argv, options in container.calls:
            assert argv[:-1] == list(spec.shell)
            assert options["workdir"] == spec.workdir and options["environment"] == spec.env
            assert not options["privileged"] and not options["tty"]
            assert spec.instruction not in argv
        assert "prepare-a" not in repr(result)
    assert container.removed


def test_prepared_image_can_have_no_extra_setup_and_still_requires_checks():
    client, spec = Client(), inputs(env={"BASH_ENV": "/root/.bashrc"})
    setup_runtime(client)
    with create_preload_environment(spec, client=client) as environment:
        result = initialize_preload_environment(environment, spec, plan(commands=()))
        assert result.commands == ()
        assert [value.command for value in result.checks].count('test -r "$BASH_ENV"') == 2


@pytest.mark.parametrize("phase,command", [("prepare", "prepare-a"), ("check", "ready")])
def test_nonzero_step_discards_container_even_when_caught_inside_the_context(phase, command):
    client, spec = Client(), inputs()
    setup_runtime(client)
    with create_preload_environment(spec, client=client) as environment:
        container = environment.container
        container.responses[command] = SimpleNamespace(exit_code=7, output=b"private details")
        with pytest.raises(PreloadInitializationError) as caught:
            initialize_preload_environment(environment, spec, plan())
        assert caught.value.phase == phase and caught.value.result.exit_code == 7
        assert "private details" not in str(caught.value)
        assert container.removed
        if phase == "prepare":
            assert "prepare-b" not in [argv[-1] for argv, _ in container.calls]
            assert "ready" not in [argv[-1] for argv, _ in container.calls]


def test_effective_workdir_must_match_before_preparation():
    client, spec = Client(), inputs()
    setup_runtime(client)
    with create_preload_environment(spec, client=client) as environment:
        container = environment.container
        container.responses["pwd -P"] = SimpleNamespace(exit_code=0, output=b"/wrong\n")
        with pytest.raises(EnvironmentMismatch, match="workdir"):
            initialize_preload_environment(environment, spec, plan())
        assert container.removed
        assert len(container.calls) == 1


def test_preparation_cannot_invalidate_the_final_workdir():
    client, spec = Client(), inputs()
    setup_runtime(client)
    with create_preload_environment(spec, client=client) as environment:
        container = environment.container

        def change_workdir(command):
            if command == "prepare-b":
                container.responses["pwd -P"] = SimpleNamespace(exit_code=0, output=b"/wrong\n")

        container.after_exec = change_workdir
        with pytest.raises(EnvironmentMismatch, match="workdir"):
            initialize_preload_environment(environment, spec, plan())
        assert container.removed


def test_command_timeout_removes_container_and_never_runs_the_next_command():
    client, spec = Client(), inputs(timeout_sec=0.1)
    setup_runtime(client)
    with create_preload_environment(spec, client=client) as environment:
        with pytest.raises(SandboxTimeout, match="prepare"):
            initialize_preload_environment(
                environment, spec, plan(commands=("hang", "must-not-run")),
            )
        container = environment.container
        assert container.exec_entered.is_set() and container.exec_stopped.is_set()
        assert container.removed
        assert "must-not-run" not in [argv[-1] for argv, _ in container.calls]


def test_whole_stage_deadline_also_bounds_an_individual_command():
    client, spec = Client(), inputs(timeout_sec=60)
    setup_runtime(client)
    with create_preload_environment(spec, client=client) as environment:
        with pytest.raises(SandboxTimeout):
            initialize_preload_environment(
                environment, spec, plan(commands=("hang",), timeout_sec=0.1),
            )
        assert environment.container.removed


@pytest.mark.parametrize("error", [OSError("lost exec reply"), KeyboardInterrupt()])
def test_unknown_command_outcome_or_cancellation_is_not_retried(error):
    client, spec = Client(), inputs()
    setup_runtime(client)
    with create_preload_environment(spec, client=client) as environment:
        container = environment.container
        container.responses["prepare-a"] = error
        expected = KeyboardInterrupt if isinstance(error, KeyboardInterrupt) else SandboxExecutionError
        with pytest.raises(expected):
            initialize_preload_environment(environment, spec, plan())
        assert container.removed
        assert [argv[-1] for argv, _ in container.calls].count("prepare-a") == 1


@pytest.mark.parametrize("exit_code,output", [(None, b""), (0, None), (True, b"ok")])
def test_incomplete_exec_results_never_become_ready(exit_code, output):
    client, spec = Client(), inputs()
    setup_runtime(client)
    with create_preload_environment(spec, client=client) as environment:
        environment.container.responses["prepare-a"] = SimpleNamespace(
            exit_code=exit_code, output=output,
        )
        with pytest.raises(SandboxExecutionError):
            initialize_preload_environment(environment, spec, plan())
        assert environment.container.removed


@pytest.mark.parametrize("failure", ["api", "exited"])
def test_startup_failure_discards_the_environment(failure):
    client, spec = Client(), inputs()
    setup_runtime(client)
    with create_preload_environment(spec, client=client) as environment:
        container = environment.container
        if failure == "api":
            container.start_failure = OSError("start outcome unknown")
        else:
            container.start_status = "exited"
        with pytest.raises(SandboxExecutionError if failure == "api" else EnvironmentMismatch):
            initialize_preload_environment(environment, spec, plan())
        assert container.removed and not container.calls


def test_successful_environment_cannot_be_initialized_a_second_time():
    client, spec = Client(), inputs()
    setup_runtime(client)
    with create_preload_environment(spec, client=client) as environment:
        initialize_preload_environment(environment, spec, plan())
        with pytest.raises(EnvironmentMismatch):
            initialize_preload_environment(environment, spec, plan())
        assert environment.container.starts == 1


def test_mismatched_environment_handle_is_rejected_before_starting():
    client, spec = Client(), inputs()
    setup_runtime(client)
    with create_preload_environment(spec, client=client) as environment:
        with pytest.raises(EnvironmentMismatch):
            initialize_preload_environment(replace(environment, container_id="wrong"), spec, plan())
        assert environment.container.starts == 0


def test_digest_input_is_checked_against_the_acquired_image():
    client, spec = Client(), inputs(image=DIGEST)
    setup_runtime(client)
    with create_preload_environment(spec, client=client) as environment:
        client.image.id = OTHER_ID
        with pytest.raises(EnvironmentMismatch, match="different image"):
            initialize_preload_environment(environment, spec, plan())
        assert environment.container.removed and environment.container.starts == 0


def test_final_container_identity_changes_never_produce_ready():
    client, spec = Client(), inputs()
    setup_runtime(client)
    with create_preload_environment(spec, client=client) as environment:
        container = environment.container

        def change_image(command):
            if command == "ready":
                container.attrs["Image"] = OTHER_ID

        container.after_exec = change_image
        with pytest.raises(EnvironmentMismatch):
            initialize_preload_environment(environment, spec, plan())
        assert container.removed


def test_cleanup_failure_preserves_primary_initialization_failure():
    client, spec = Client(), inputs()
    setup_runtime(client)
    with create_preload_environment(spec, client=client) as environment:
        container = environment.container
        container.responses["prepare-a"] = SimpleNamespace(exit_code=5, output=b"bad")
        client.remove_error = True
        with pytest.raises(PreloadCleanupError) as caught:
            initialize_preload_environment(environment, spec, plan())
        assert isinstance(caught.value.__cause__, PreloadInitializationError)
        assert caught.value.container_id == environment.container_id
        client.remove_error = False  # Let the outer owner finish cleanup.
    assert container.removed


@pytest.mark.parametrize("changes", [
    {"checks": ()}, {"checks": ("",)}, {"checks": ("bad\x00command",)},
    {"commands": (" ",)}, {"commands": "echo not-an-array"},
    {"timeout_sec": 0}, {"timeout_sec": -1}, {"timeout_sec": True},
    {"timeout_sec": float("inf")}, {"unknown": True},
])
def test_initialization_plan_requires_valid_commands_checks_and_deadlines(changes):
    with pytest.raises(ValidationError):
        plan(**changes)
