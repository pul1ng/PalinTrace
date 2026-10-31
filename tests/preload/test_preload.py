import hashlib
import json
import stat
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

import palinreplay.preload.build as preload_build
import palinreplay.storage.btrfs.pool as btrfs_pool
from palinreplay.preload import build_preload
from palinreplay.preload.base import open_preload
from palinreplay.preload.environment import PreloadCleanupError
from palinreplay.preload.initialization import InitializationSpec, PreloadInitializationError
from palinreplay.storage.btrfs.models import PoolLocation
from palinreplay.storage.store import IntegrityError
from tests.preload.test_preload_environment import Client, inputs
from tests.preload.test_preload_initialization import setup_runtime
from tests.preload.test_preload_input import prepared_package
from tests.storage.test_rootfs_manifest import files, tar_bytes


@pytest.fixture
def runtime(monkeypatch):
    client = Client()
    tools = SimpleNamespace(entered=0, closed=0, corrupt=False, pools=[])
    client.payload = b"initial C0"
    client.fail_prepare = False
    setup_runtime(client)
    configure_runtime = client.on_create

    def configure(container):
        configure_runtime(container)
        container.id = hashlib.sha256(container.options["name"].encode()).hexdigest()
        container.attrs["Config"].update(User="", Entrypoint=["sleep"], Cmd=["infinity"])
        container.attrs["HostConfig"] = {
            "NetworkMode": container.options["network_mode"], "Memory": 2 * 1024**3,
            "NanoCpus": 2_000_000_000, "IpcMode": "private",
        }
        container.attrs["State"].update(StartedAt="start", FinishedAt="", Pid=123, ExitCode=0)
        container.attrs["RestartCount"] = 0
        if client.fail_prepare:
            container.responses["prepare"] = SimpleNamespace(exit_code=2, output=b"private details")
        data = tar_bytes(files(client.payload))

        def stop(**kwargs):
            assert kwargs == {"timeout": 5}
            container.attrs["State"].update(
                Status="exited", Running=False, Paused=False, Restarting=False,
                FinishedAt="stop", Pid=0,
            )

        container.stop = stop
        container.export = lambda chunk_size: iter([data])

    client.on_create = configure
    class Pool:
        def __init__(self):
            identity = uuid4()
            self.location = PoolLocation(
                volume="palinreplay-btrfs-" + identity.hex, uuid=identity,
                daemon_id="fixture", helper_image_id="sha256:" + "a" * 64,
            )
            self.initial_uploads = ()
            self.active = False
            self.discarded = False
            tools.pools.append(self)

        def __enter__(self):
            self.active = True
            tools.entered += 1
            return self

        def command(self, op, **kwargs):
            if op == "import":
                if tools.corrupt:
                    raise IntegrityError("Btrfs imported files differ")
                oracle = next(path for path, name in self.initial_uploads if name == "expected-C0.json")
                data = oracle.read_bytes()
                return {
                    "oracle_sha256": hashlib.sha256(data).hexdigest(),
                    "paths": len(json.loads(data)["entries"]),
                }
            if op == "usage":
                return {"allocated_bytes": 4096}
            assert op == "delete"
            return {}

        def snapshot(self, source, target, *, readonly=True):
            assert readonly and self.active
            return {"uuid": str(uuid4()), "readonly": True}

        def close(self):
            if self.active:
                tools.closed += 1
                self.active = False

        def discard(self):
            self.close()
            self.discarded = True

    monkeypatch.setattr(btrfs_pool.BtrfsPool, "create", lambda client: Pool())
    return client, tools


def plan():
    return InitializationSpec(commands=("prepare",), checks=("ready",))


def build(source, output, runtime, **kwargs):
    return build_preload(
        source, output, initialization=plan(), client=runtime[0],
        **({"public_env_keys": ("LANG", "PATH")} | kwargs),
    )


@pytest.mark.parametrize("source_kind", ["object", "json", "task-package"])
def test_all_input_forms_build_a_verified_readonly_base(tmp_path, row, runtime, source_kind):
    source, keys = inputs(), ("LANG", "PATH")
    if source_kind == "json":
        path = tmp_path / "preload-input.json"
        path.write_text(source.model_dump_json())
        source = path
    elif source_kind == "task-package":
        source = prepared_package(tmp_path, row)
        keys = ("PATH", "BASH_ENV")
    result = build(source, tmp_path / "base", runtime, public_env_keys=keys)
    frozen = open_preload(result.path)
    assert result.path == (tmp_path / "base").absolute()
    assert result.preload_id == "preload:sha256:" + frozen.metadata_sha256
    assert result.duration_sec >= 0
    for path in result.path.iterdir():
        assert not stat.S_IMODE(path.stat().st_mode) & 0o222
    assert all(container.removed for container in runtime[0].created)
    assert not runtime[0].closed  # Borrowed clients remain caller-owned.
    assert runtime[1].entered == runtime[1].closed == 1


def test_task_package_selection_is_forwarded_to_input_loading(tmp_path, row, runtime):
    source = prepared_package(tmp_path, row, two=True)
    with pytest.raises(ValueError, match="task_id is required"):
        build(source, tmp_path / "missing-selection", runtime)
    assert not runtime[0].created
    result = build(
        source, tmp_path / "selected", runtime, task_id="example__repo-2",
        public_env_keys=("PATH", "BASH_ENV"),
    )
    assert open_preload(result.path).metadata.task_id == "example__repo-2"


def test_preload_id_binds_the_pool_and_readonly_snapshot(tmp_path, runtime):
    first = build(inputs(), tmp_path / "one", runtime)
    second = build(inputs(), tmp_path / "two", runtime)
    a, b = open_preload(first.path), open_preload(second.path)
    assert first.preload_id != second.preload_id
    assert a.metadata.snapshot.location != b.metadata.snapshot.location
    assert a.metadata.input_sha256 == b.metadata.input_sha256
    assert a.metadata.snapshot.name == b.metadata.snapshot.name == "bases/C0"


def test_existing_output_is_rejected_before_creating_docker_resources(tmp_path, runtime):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "sentinel").write_text("keep")
    with pytest.raises(FileExistsError):
        build(inputs(), output, runtime)
    assert (output / "sentinel").read_text() == "keep"
    assert not runtime[0].created and not runtime[0].image_gets


def test_input_validation_failure_does_not_create_outputs_or_containers(tmp_path, runtime):
    source = tmp_path / "bad.json"
    source.write_text(json.dumps({"image": "moving:tag"}))
    with pytest.raises(ValueError):
        build(source, tmp_path / "base", runtime)
    assert not (tmp_path / "base").exists() and not runtime[0].created


def test_task_selector_cannot_silently_relabel_standalone_input(tmp_path, runtime):
    with pytest.raises(ValueError, match="task_id does not match"):
        build(inputs(), tmp_path / "base", runtime, task_id="other-task")
    assert not runtime[0].created


def test_initialization_failure_does_not_publish_an_id_or_output(tmp_path, runtime):
    runtime[0].fail_prepare = True
    with pytest.raises(PreloadInitializationError):
        build(inputs(), tmp_path / "base", runtime)
    assert not (tmp_path / "base").exists()
    assert runtime[0].created[0].removed and runtime[1].entered == 0


def test_freeze_failure_removes_the_private_output_and_container(tmp_path, runtime):
    runtime[1].corrupt = True
    with pytest.raises(IntegrityError):
        build(inputs(), tmp_path / "base", runtime)
    assert not (tmp_path / "base").exists()
    assert not list(tmp_path.glob(".record-export-*"))
    assert runtime[0].created[0].removed


def test_cleanup_failure_cannot_publish_the_already_frozen_private_base(tmp_path, runtime):
    client, _ = runtime
    client.remove_error = True
    try:
        with pytest.raises(PreloadCleanupError):
            build(inputs(), tmp_path / "base", runtime)
        assert not (tmp_path / "base").exists()
        assert not list(tmp_path.glob(".record-export-*"))
        assert not client.created[0].removed
    finally:
        client.remove_error = False
        for container in client.created:
            container.remove(force=True, v=True)


def test_publication_happens_after_helper_and_container_cleanup(tmp_path, runtime, monkeypatch):
    client, _ = runtime
    monkeypatch.setattr(preload_build, "connect_docker", lambda timeout: client)
    publish = preload_build.export_directory

    @contextmanager
    def check_cleanup_before_publish(output):
        with publish(output) as stage:
            yield stage
            assert all(container.removed for container in client.created)
            assert runtime[1].entered == runtime[1].closed
            assert client.closed

    monkeypatch.setattr(preload_build, "export_directory", check_cleanup_before_publish)
    result = build_preload(
        inputs(), tmp_path / "base", initialization=plan(), public_env_keys=("LANG", "PATH"),
    )
    assert result.path.is_dir()


def test_metadata_write_failure_withdraws_output_and_discards_new_pool(tmp_path, runtime, monkeypatch):
    output = tmp_path / "base"
    publish = preload_build.write_json

    def fail(path, value):
        if path.name == "c0.checksum.json":
            raise OSError("metadata publication failed")
        return publish(path, value)

    monkeypatch.setattr(preload_build, "write_json", fail)
    with pytest.raises(OSError, match="metadata publication"):
        build(inputs(), output, runtime)
    assert not output.exists() and runtime[1].pools[0].discarded
    assert all(container.removed for container in runtime[0].created)


def test_environment_options_are_forwarded(tmp_path, runtime):
    result = build(
        inputs(), tmp_path / "base", runtime, allow_pull=False, network_mode="none",
        api_timeout_sec=45,
    )
    assert runtime[0].created[0].options["network_mode"] == "none"
    assert open_preload(result.path).metadata.launch.network_mode == "none"


@pytest.mark.parametrize("options", [
    {"public_env_keys": ["LANG"]}, {"public_env_keys": ("LANG", "LANG")},
    {"public_env_keys": ([],)}, {"stop_timeout_sec": -1},
    {"stop_timeout_sec": True},
    {"allow_pull": 1}, {"network_mode": "host"}, {"api_timeout_sec": 0},
])
def test_invalid_options_fail_without_docker_resources(tmp_path, runtime, options):
    with pytest.raises(ValueError):
        build(inputs(), tmp_path / "base", runtime, **options)
    assert not runtime[0].created
    assert not (tmp_path / "base").exists()
