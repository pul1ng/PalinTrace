import json
import signal
import tomllib
from pathlib import Path

import pytest

import palinreplay.cli.app as cli
import palinreplay.preload.batch as preload_batch
import palinreplay.preload.build as preload_build
import palinreplay.preload.environment as preload_environment
from palinreplay.preload.base import BtrfsPreloadRecord, open_preload
from palinreplay.sandbox.binding import load_sandbox_task
from tests.preload.test_preload import runtime as runtime
from tests.preload.test_preload_batch import batch_package as batch_package
from tests.preload.test_preload_environment import Client, inputs
from tests.preload.test_preload_input import prepared_package


@pytest.fixture
def cli_runtime(monkeypatch, runtime):
    inspector = Client()
    inspector.image.attrs["Config"]["Env"] = ["PATH=/usr/bin", "PYTHON_VERSION=3.11"]
    configure_runtime = runtime[0].on_create

    def configure(container):
        configure_runtime(container)
        container.attrs["Config"]["Env"].append("PYTHON_VERSION=3.11")

    runtime[0].on_create = configure
    monkeypatch.setattr(preload_environment, "connect_docker", lambda timeout: inspector)
    monkeypatch.setattr(preload_build, "connect_docker", lambda timeout: runtime[0])
    return inspector, runtime[0], runtime[1]


def source_json(tmp_path, **kwargs):
    source = tmp_path / "input.json"
    source.write_text(inputs(**kwargs).model_dump_json())
    return source


def run(source, output, *flags):
    return cli.main([
        "preload", str(source), "--output", str(output), *flags,
    ])


def test_command_builds_btrfs_base_without_executing_instruction(
    tmp_path, cli_runtime, capsys, monkeypatch,
):
    monkeypatch.setenv("HOST_PRIVATE", "host-secret")
    source = source_json(tmp_path, env={"LANG": "C.UTF-8", "PRIVATE_TOKEN": "input-secret"})
    original = source.read_bytes()
    output = tmp_path / "base"
    assert run(source, output) == 0
    frozen = open_preload(output)
    assert frozen.metadata.launch.environment == {
        "LANG": "C.UTF-8", "PATH": "/usr/bin", "PYTHON_VERSION": "3.11",
    }
    assert frozen.metadata.launch.omitted_environment_keys == ("PRIVATE_TOKEN",)
    assert source.read_bytes() == original
    message = capsys.readouterr()
    assert message.err == ""
    assert f"Preload: preload:sha256:{frozen.metadata_sha256}" in message.out
    assert f"Path: {output.absolute()}" in message.out and "Time: " in message.out
    assert "input-secret" not in message.out and "host-secret" not in message.out
    inspector, builder, tools = cli_runtime
    assert inspector.closed and builder.closed and not inspector.created
    assert len(builder.created) == 1 and builder.created[0].removed
    assert tools.entered == tools.closed
    calls = [argv[-1] for argv, _ in builder.created[0].calls]
    assert calls and set(calls) == {"pwd -P"}
    assert inputs().instruction not in calls


def test_custom_initialization_and_repeatable_public_env(tmp_path, cli_runtime, capsys):
    source = source_json(tmp_path, env={"LANG": "C.UTF-8", "CUSTOM": "approved"})
    plan = tmp_path / "init.json"
    plan.write_text(json.dumps({"commands": ["prepare"], "checks": ["ready"], "timeout_sec": 20}))
    output = tmp_path / "base"
    assert run(
        source, output, "--init", str(plan), "--public-env", "CUSTOM", "--public-env", "CUSTOM",
    ) == 0
    frozen = open_preload(output)
    assert frozen.metadata.launch.environment["CUSTOM"] == "approved"
    calls = [argv[-1] for argv, _ in cli_runtime[1].created[0].calls]
    assert calls.count("prepare") == 1 and calls.count("ready") == 2
    assert not capsys.readouterr().err


def test_default_preload_output_uses_task_identity(tmp_path, cli_runtime, monkeypatch):
    import palinreplay.cli.outputs as outputs

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(outputs, "timestamp", lambda: "261031_131022")
    source = source_json(tmp_path, task_id="astropy__astropy-12907")
    assert cli.main(["preload", str(source)]) == 0
    path = tmp_path / "outputs/preload/261031_131022_astropy__astropy-12907"
    assert open_preload(path).metadata.task_id == "astropy__astropy-12907"


def test_native_package_task_selection(tmp_path, row, cli_runtime, capsys):
    source = prepared_package(tmp_path, row, two=True)
    output = tmp_path / "base"
    assert run(source, output, "--task-id", "example__repo-2") == 0
    frozen = open_preload(output)
    assert frozen.metadata.task_id == "example__repo-2"
    assert "BASH_ENV" in frozen.metadata.launch.environment


@pytest.mark.parametrize("selected", [[], ["example__repo-3", "example__repo-1"]])
def test_batch_cli_selects_tasks_and_passes_configuration(
    tmp_path, batch_package, cli_runtime, selected,
):
    output = tmp_path / "batch"
    init = tmp_path / "init.json"
    init.write_text(json.dumps({"commands": ["prepare"], "checks": ["ready"]}))
    flags = [flag for task_id in selected for flag in ("--task-id", task_id)]
    assert run(batch_package, output, *flags, "--init", str(init), "--public-env", "BASH_ENV") == 0
    expected = selected or ["example__repo-1", "example__repo-2", "example__repo-3"]
    index = json.loads((output / "preloads.json").read_text())
    assert [entry["task_id"] for entry in index["tasks"]] == expected
    assert [entry["status"] for entry in index["tasks"]] == ["completed"] * len(expected)
    for entry, task_id in zip(index["tasks"], expected, strict=True):
        frozen = open_preload(output / task_id)
        assert frozen.metadata.task_id == task_id
        assert "BASH_ENV" in frozen.metadata.launch.environment
        assert entry["preload_id"] == "preload:sha256:" + frozen.metadata_sha256
    inspector, builder, tools = cli_runtime
    assert len(inspector.image_gets) == len(builder.created) == len(tools.pools) == len(expected)
    assert all(container.removed for container in builder.created)
    assert tools.entered == tools.closed == len(expected)
    for container in builder.created:
        calls = [argv[-1] for argv, _ in container.calls]
        assert calls.count("prepare") == 1 and calls.count("ready") == 2


def test_batch_progress_reports_order_paths_durations_and_summary(
    tmp_path, batch_package, cli_runtime, capsys,
):
    output = tmp_path / "batch"
    assert run(
        batch_package, output, "--task-id", "example__repo-3", "--task-id", "example__repo-1",
    ) == 0
    message = capsys.readouterr()
    assert message.err == ""
    index = json.loads((output / "preloads.json").read_text())
    positions = []
    for number, entry in enumerate(index["tasks"], start=1):
        task_id = entry["task_id"]
        progress = f"[{number}/2] {task_id}"
        result = f"{task_id}: {output / entry['path']} ({entry['duration_sec']:.3f}s)"
        positions.extend([message.out.index(progress), message.out.index(result)])
    assert positions == sorted(positions)
    assert f"Batch: {output.absolute()}" in message.out
    assert "Completed: 2/2" in message.out and "Status: completed" in message.out
    assert "Time: " in message.out


def test_single_task_package_without_selector_keeps_root_layout(tmp_path, row, cli_runtime):
    source = prepared_package(tmp_path, row)
    output = tmp_path / "base"
    assert run(source, output) == 0
    assert open_preload(output).metadata.task_id == row["instance_id"]
    assert not (output / "preloads.json").exists()


@pytest.mark.parametrize("flags", [
    ["--task-id", "example__repo-1", "--task-id", "example__repo-1"],
    ["--task-id", "example__repo-1", "--task-id", "missing"],
])
def test_batch_invalid_ids_fail_before_docker(tmp_path, batch_package, cli_runtime, flags):
    output = tmp_path / "batch"
    assert run(batch_package, output, *flags) == 1
    assert not output.exists() and not cli_runtime[0].image_gets and not cli_runtime[1].created


def test_json_multiple_ids_fail_before_docker(tmp_path, cli_runtime):
    output = tmp_path / "base"
    source = source_json(tmp_path, task_id="example__repo-1")
    assert run(source, output, "--task-id", "example__repo-1", "--task-id", "other") == 1
    assert not output.exists() and not cli_runtime[0].image_gets and not cli_runtime[1].created


def test_batch_failure_preserves_success_and_stops(tmp_path, batch_package, cli_runtime, capsys):
    builder = cli_runtime[1]
    configure = builder.on_create

    def fail_second(container):
        configure(container)
        if len(builder.created) == 2:
            container.start_failure = RuntimeError("private error details")

    builder.on_create = fail_second
    output = tmp_path / "batch"
    assert run(batch_package, output) == 1
    message = capsys.readouterr()
    assert "private error details" not in message.err
    assert "Completed: 1/3" in message.out and "Status: failed" in message.out
    assert "[3/3]" not in message.out
    index = json.loads((output / "preloads.json").read_text())
    assert index["status"] == "failed"
    assert [entry["status"] for entry in index["tasks"]] == ["completed", "failed", "pending"]
    first = output / "example__repo-1"
    binding = load_sandbox_task(batch_package, "example__repo-1")
    assert BtrfsPreloadRecord.select(first, binding).base.metadata.task_id == "example__repo-1"
    assert len(builder.created) == 2 and all(container.removed for container in builder.created)
    assert not (output / "example__repo-3").exists()


def test_batch_index_failure_keeps_published_base_and_returns_failure(
    tmp_path, batch_package, cli_runtime, capsys, monkeypatch,
):
    write_json = preload_batch.write_json

    def fail_after_first(path, value, **kwargs):
        if value["tasks"][0]["status"] == "completed":
            raise OSError("private filesystem details")
        write_json(path, value, **kwargs)

    monkeypatch.setattr(preload_batch, "write_json", fail_after_first)
    output = tmp_path / "batch"
    assert run(batch_package, output) == 1
    message = capsys.readouterr()
    assert "private filesystem details" not in message.err
    assert "Completed: 1/3" in message.out and "Status: failed" in message.out
    assert "OSError" in message.err
    first = output / "example__repo-1"
    assert BtrfsPreloadRecord.select(
        first, load_sandbox_task(batch_package, "example__repo-1"),
    ).base.metadata.task_id == "example__repo-1"
    assert len(cli_runtime[1].created) == 1 and not cli_runtime[2].pools[0].discarded
    index = json.loads((output / "preloads.json").read_text())
    assert [entry["status"] for entry in index["tasks"]] == ["building", "pending", "pending"]


@pytest.mark.parametrize("when, expected_builds", [("initial", 0), ("final", 3)])
def test_batch_index_failure_at_boundaries_never_claims_success(
    tmp_path, batch_package, cli_runtime, capsys, monkeypatch, when, expected_builds,
):
    write_json = preload_batch.write_json

    def fail_index(path, value, **kwargs):
        initial = all(entry["status"] == "pending" for entry in value["tasks"])
        final = value["status"] == "completed"
        if (when == "initial" and initial) or (when == "final" and final):
            raise OSError("index unavailable")
        write_json(path, value, **kwargs)

    monkeypatch.setattr(preload_batch, "write_json", fail_index)
    output = tmp_path / "batch"
    assert run(batch_package, output) == 1
    message = capsys.readouterr()
    assert "OSError" in message.err and "Status: failed" in message.out
    assert f"Completed: {expected_builds}/3" in message.out
    assert len(cli_runtime[1].created) == expected_builds
    if expected_builds:
        assert all(open_preload(output / f"example__repo-{number}") for number in range(1, 4))
        assert json.loads((output / "preloads.json").read_text())["status"] == "running"


def test_batch_cancellation_keeps_first_base_and_stops(
    tmp_path, batch_package, cli_runtime, capsys,
):
    builder = cli_runtime[1]
    configure = builder.on_create

    def cancel_second(container):
        configure(container)
        if len(builder.created) == 2:
            container.start_failure = KeyboardInterrupt()

    builder.on_create = cancel_second
    output = tmp_path / "batch"
    assert run(batch_package, output) == 130
    message = capsys.readouterr()
    assert "cancelled" in message.err and "Status: cancelled" in message.out
    assert "Completed: 1/3" in message.out and "[3/3]" not in message.out
    index = json.loads((output / "preloads.json").read_text())
    assert index["status"] == "cancelled"
    assert [entry["status"] for entry in index["tasks"]] == ["completed", "cancelled", "pending"]
    assert BtrfsPreloadRecord.select(
        output / "example__repo-1", load_sandbox_task(batch_package, "example__repo-1"),
    ).base.metadata.task_id == "example__repo-1"
    assert len(builder.created) == 2 and all(container.removed for container in builder.created)
    assert not (output / "example__repo-3").exists()


def test_batch_cancellation_during_index_update_records_completed_base(
    tmp_path, batch_package, cli_runtime, capsys, monkeypatch,
):
    write_json = preload_batch.write_json
    interrupted = False

    def interrupt_after_first(path, value, **kwargs):
        nonlocal interrupted
        if value["tasks"][0]["status"] == "completed" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt()
        write_json(path, value, **kwargs)

    monkeypatch.setattr(preload_batch, "write_json", interrupt_after_first)
    output = tmp_path / "batch"
    assert run(batch_package, output) == 130
    assert "cancelled" in capsys.readouterr().err
    index = json.loads((output / "preloads.json").read_text())
    assert index["status"] == "cancelled"
    assert [entry["status"] for entry in index["tasks"]] == ["completed", "pending", "pending"]
    assert open_preload(output / "example__repo-1").metadata.task_id == "example__repo-1"
    assert len(cli_runtime[1].created) == 1


def test_batch_cleanup_failure_supersedes_cancellation(tmp_path, batch_package, cli_runtime, capsys):
    builder = cli_runtime[1]
    configure = builder.on_create

    def cancel_and_fail_cleanup(container):
        configure(container)
        if len(builder.created) == 2:
            builder.remove_error = True
            container.start_failure = KeyboardInterrupt()

    builder.on_create = cancel_and_fail_cleanup
    output = tmp_path / "batch"
    assert run(batch_package, output) == 1
    assert "removal unconfirmed" in capsys.readouterr().err
    index = json.loads((output / "preloads.json").read_text())
    assert index["status"] == "failed"
    assert [entry["status"] for entry in index["tasks"]] == ["completed", "failed", "pending"]
    assert open_preload(output / "example__repo-1").metadata.task_id == "example__repo-1"
    assert len(builder.created) == 2 and not cli_runtime[2].pools[0].discarded


def test_json_input_cannot_be_relabelled_with_task_id(tmp_path, cli_runtime, capsys):
    assert run(source_json(tmp_path), tmp_path / "base", "--task-id", "wrong-task") == 1
    assert "--task-id" in capsys.readouterr().err
    assert not cli_runtime[0].image_gets and not cli_runtime[1].created


@pytest.mark.parametrize("contents", [
    "not JSON", "[]", '{"checks": []}', '{"commands": ["prepare"]}',
    '{"checks": ["ready"], "checks": ["different"]}',
    '{"checks": ["ready"], "timeout_sec": true}',
    '{"checks": ["ready"], "unknown-secret-field": "private-command"}',
])
def test_invalid_init_fails_before_docker_without_disclosing_input(
    tmp_path, cli_runtime, capsys, contents,
):
    plan = tmp_path / "init.json"
    plan.write_text(contents)
    output = tmp_path / "base"
    assert run(source_json(tmp_path), output, "--init", str(plan)) == 1
    message = capsys.readouterr()
    assert message.out == "" and "error:" in message.err
    assert "private-command" not in message.err and "unknown-secret-field" not in message.err
    assert not output.exists() and not cli_runtime[0].image_gets and not cli_runtime[1].created


@pytest.mark.parametrize("kind", ["invalid-json", "invalid-input", "missing-file"])
def test_invalid_source_fails_before_docker(tmp_path, cli_runtime, capsys, kind):
    source = tmp_path / "input.json"
    if kind != "missing-file":
        source.write_text("{" if kind == "invalid-json" else '{"instruction": "private-task"}')
    assert run(source, tmp_path / "base") == 1
    message = capsys.readouterr()
    assert message.out == "" and "private-task" not in message.err
    assert not cli_runtime[0].image_gets and not cli_runtime[1].created


@pytest.mark.parametrize("symlink", [False, True])
def test_existing_output_is_preserved_before_docker(tmp_path, cli_runtime, capsys, symlink):
    source = source_json(tmp_path)
    output = tmp_path / "base"
    if symlink:
        output.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    else:
        output.mkdir()
        (output / "sentinel").write_text("keep")
    assert run(source, output) == 1
    assert "output already exists" in capsys.readouterr().err
    if symlink:
        assert output.is_symlink()
    else:
        assert (output / "sentinel").read_text() == "keep"
    assert not cli_runtime[0].image_gets and not cli_runtime[1].created


@pytest.mark.parametrize("key", ["", "TOKEN=secret", "bad\x00key"])
def test_invalid_public_names_fail_before_docker(tmp_path, cli_runtime, capsys, key):
    assert run(source_json(tmp_path), tmp_path / "base", "--public-env", key) == 1
    assert "without values" in capsys.readouterr().err
    assert not cli_runtime[0].image_gets and not cli_runtime[1].created


def test_absent_public_key_fails_before_creating_container(tmp_path, cli_runtime, capsys):
    output = tmp_path / "base"
    assert run(source_json(tmp_path), output, "--public-env", "MISSING") == 1
    assert "absent" in capsys.readouterr().err
    assert cli_runtime[0].closed and not cli_runtime[1].created and not output.exists()


@pytest.mark.parametrize("close_failure", [False, True])
def test_image_inspection_failure_closes_client_without_publication(
    tmp_path, cli_runtime, capsys, close_failure,
):
    inspector = cli_runtime[0]
    if close_failure:
        inspector.close_error = True
    else:
        inspector.inspect_error = RuntimeError("credential-secret")
    output = tmp_path / "base"
    assert run(source_json(tmp_path), output) == 1
    message = capsys.readouterr()
    assert message.out == "" and "credential-secret" not in message.err
    assert inspector.closed and not cli_runtime[1].created and not output.exists()


def test_preparation_failure_removes_container_and_output(tmp_path, cli_runtime, capsys):
    cli_runtime[1].fail_prepare = True
    plan = tmp_path / "init.json"
    plan.write_text('{"commands": ["prepare"], "checks": ["ready"]}')
    output = tmp_path / "base"
    assert run(source_json(tmp_path), output, "--init", str(plan)) == 1
    message = capsys.readouterr()
    assert message.out == "" and "exit 2" in message.err
    assert "private details" not in message.err
    assert cli_runtime[1].created[0].removed and cli_runtime[1].closed
    assert not output.exists() and cli_runtime[2].entered == 0


def test_builder_client_close_failure_never_announces_success(tmp_path, cli_runtime, capsys):
    cli_runtime[1].close_error = True
    output = tmp_path / "base"
    assert run(source_json(tmp_path), output) == 1
    message = capsys.readouterr()
    assert message.out == "" and "SandboxCleanupError" in message.err
    assert not output.exists() and cli_runtime[1].created[0].removed


def test_cancel_during_build_cleans_owned_resources(tmp_path, cli_runtime, capsys):
    configure_runtime = cli_runtime[1].on_create

    def configure(container):
        configure_runtime(container)
        container.start_failure = KeyboardInterrupt()

    cli_runtime[1].on_create = configure
    output = tmp_path / "base"
    assert run(source_json(tmp_path), output) == 130
    message = capsys.readouterr()
    assert message.out == "" and "cancelled" in message.err
    assert cli_runtime[0].closed and cli_runtime[1].closed
    assert cli_runtime[1].created[0].removed and not output.exists()


def test_sigterm_is_cancelled_and_previous_handler_is_restored(tmp_path, cli_runtime, capsys):
    previous = signal.getsignal(signal.SIGTERM)
    cli_runtime[0].on_get_image = lambda: signal.raise_signal(signal.SIGTERM)
    assert run(source_json(tmp_path), tmp_path / "base") == 130
    assert "cancelled" in capsys.readouterr().err
    assert signal.getsignal(signal.SIGTERM) == previous
    assert cli_runtime[0].closed and not cli_runtime[1].created


def test_preload_help_lists_mvp_flags_without_optional_agent_or_docker_calls(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["preload", "--help"])
    assert error.value.code == 0
    message = capsys.readouterr().out
    for flag in ("--output", "--task-id", "--init", "--public-env"):
        assert flag in message
    assert "reload" not in message.replace("preload", "")


def test_reload_is_not_a_command(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["reload", "input.json", "--output", "base"])
    assert error.value.code == 2
    assert "invalid choice: 'reload'" in capsys.readouterr().err


def test_palin_console_script_uses_existing_dispatcher():
    project = Path(__file__).parents[2] / "pyproject.toml"
    scripts = tomllib.loads(project.read_text())["project"]["scripts"]
    assert scripts["palin"] == scripts["palinreplay"] == "palinreplay.cli:main"
