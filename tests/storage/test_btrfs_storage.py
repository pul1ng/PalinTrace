import json
import os
import tarfile
from types import SimpleNamespace
from uuid import uuid4

import pytest

import palinreplay.preload.base as preload_base
import palinreplay.preload.build as preload_build
import palinreplay.record.state as record_state
import palinreplay.storage.btrfs.models as btrfs_models
import palinreplay.storage.btrfs.pool as btrfs_pool
import palinreplay.storage.btrfs.worker as _btrfs_worker
import palinreplay.storage.rootfs as _rootfs
from palinreplay.cli import build_parser
from palinreplay.core.errors import RecordUsageError
from palinreplay.core.interfaces import AgentSnapshot, BoundaryAdapter, Payload
from palinreplay.core.models import State, StateCapture
from palinreplay.preload import build_preload
from palinreplay.preload.initialization import InitializationSpec
from palinreplay.record.inspect import export_record
from palinreplay.record.recorder import Recorder
from palinreplay.record.restore import RestoredNode, restore_node
from palinreplay.record.run import RunOptions
from palinreplay.sandbox.base import SandboxCleanupError
from palinreplay.sandbox.runtime import C0LaunchConfig
from palinreplay.storage.btrfs.models import PoolLocation, Snapshot
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.btrfs.verification import capture_info
from palinreplay.storage.io import write_json
from palinreplay.storage.store import IntegrityError, RecordStore
from tests.preload.test_preload_environment import inputs
from tests.storage.test_store import start


@pytest.fixture
def snapshot():
    identity = uuid4()
    return Snapshot(
        location=PoolLocation(
            volume="palinreplay-btrfs-" + identity.hex,
            uuid=identity,
            daemon_id="fixture-daemon",
            helper_image_id="sha256:" + "a" * 64,
        ),
        name="bases/C0",
        uuid=uuid4(),
    )


def test_recording_requires_a_preload():
    with pytest.raises(RecordUsageError, match="requires --preload"):
        RunOptions().validate()
    RunOptions(preload="base").validate()


@pytest.mark.parametrize("command", [
    ["preload", "task", "--output", "base"],
    ["record", "run", "task", "--output", "run", "--preload", "base"],
])
def test_cli_has_no_storage_selector(command, capsys):
    parser = build_parser()
    assert not hasattr(parser.parse_args(command), "storage_format")
    with pytest.raises(SystemExit) as error:
        parser.parse_args([*command, "--storage-format", "btrfs"])
    assert error.value.code == 2
    assert "unrecognized arguments: --storage-format" in capsys.readouterr().err


def test_default_preload_dispatches_to_btrfs(tmp_path, monkeypatch):
    source, output, client = inputs(), tmp_path / "base", object()
    initialization = InitializationSpec(checks=("pwd -P",))
    calls = []

    def build(selected, path, **options):
        calls.append((selected, path, options))
        return SimpleNamespace(preload_id="preload:sha256:" + "a" * 64, path=path)

    monkeypatch.setattr(preload_build, "_build_preload", build)
    result = build_preload(
        source, output, initialization=initialization, public_env_keys=("LANG",),
        client=client,
    )
    assert result.preload_id == "preload:sha256:" + "a" * 64
    assert result.path == output and result.duration_sec >= 0
    assert calls == [(source, output, {
        "initialization": initialization, "public_env_keys": ("LANG",), "client": client,
        "allow_pull": True, "network_mode": "bridge", "api_timeout_sec": 600,
        "stop_timeout_sec": 5,
    })]


@pytest.mark.parametrize("missing", [False, True])
def test_preload_environment_reinjects_only_omitted_values(missing):
    def image(reference):
        assert reference == "sha256:" + "a" * 64
        if missing:
            error = RuntimeError()
            error.status_code = 404
            raise error
        return SimpleNamespace(attrs={"Config": {"Env": ["PRIVATE=value", "LANG=ignored"]}})

    base = SimpleNamespace(metadata=SimpleNamespace(
        source_image_id="sha256:" + "a" * 64,
        launch=SimpleNamespace(environment={"LANG": "C"}, omitted_environment_keys=("PRIVATE",)),
    ))
    selected = preload_base.BtrfsPreloadRecord(base, SimpleNamespace(env={"LANG": "C.UTF-8"}))
    client = SimpleNamespace(images=SimpleNamespace(get=image))
    if missing:
        with pytest.raises(RecordUsageError, match="omitted environment"):
            selected.environment(client)
    else:
        assert selected.environment(client) == {"LANG": "C.UTF-8", "PRIVATE": "value"}


def test_fully_supplied_preload_environment_needs_no_source_image():
    base = SimpleNamespace(metadata=SimpleNamespace(launch=SimpleNamespace(
        environment={"LANG": "C"}, omitted_environment_keys=("PRIVATE",),
    )))
    selected = preload_base.BtrfsPreloadRecord(base, SimpleNamespace(env={"PRIVATE": "injected"}))
    assert selected.environment(None) == {"LANG": "C", "PRIVATE": "injected"}


def test_restore_refuses_other_backends_before_using_docker(tmp_path):
    with RecordStore.create(tmp_path / "record") as store:
        node = start(store)[-1]
        with pytest.raises(IntegrityError, match="unsupported Btrfs"):
            restore_node(
                store, node.node_id, client=None, docker_host="unix:///fixture.sock",
                report_path=tmp_path / "restore.json",
            )
    assert not (tmp_path / "restore.json").exists()


@pytest.mark.parametrize("failure", [False, True])
def test_restore_cleanup_keeps_a_durable_report_and_closes_both_resources(tmp_path, failure):
    calls = []

    def close_workspace():
        calls.append("workspace")
        if failure:
            raise OSError("cleanup failed")

    path = tmp_path / "restore.json"
    restored = RestoredNode(None, "node", path)
    restored.workspace = SimpleNamespace(close=close_workspace)
    restored.pool = SimpleNamespace(close=lambda: calls.append("pool"))
    if failure:
        with pytest.raises(SandboxCleanupError):
            restored.close()
    else:
        restored.close()
        restored.close()
    assert calls == ["workspace", "pool"]
    report = json.loads(path.read_text())
    assert report["cleanup_confirmed"] is not failure
    assert bool(report["cleanup_errors"]) is failure


@pytest.mark.parametrize("readonly,wrong_uuid", [(False, False), (True, True)])
def test_snapshot_reference_requires_matching_uuid_and_readonly(snapshot, readonly, wrong_uuid):
    pool = BtrfsPool(None, snapshot.location)
    pool.command = lambda *args, **kwargs: {
        "uuid": str(uuid4() if wrong_uuid else snapshot.uuid),
        "readonly": readonly,
    }
    with pytest.raises(IntegrityError, match="identity or read-only"):
        pool.verify(snapshot)


def test_full_native_inventory_matches_existing_rootfs_contract(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "file").write_bytes(b"exact bytes\x00\xff")
    (root / "file").chmod(0o640)
    os.link(root / "file", root / "hardlink")
    (root / "symlink").symlink_to("file")
    (root / "empty").mkdir()
    (root / "proc").mkdir()
    (root / "proc" / "ignored").write_text("runtime mount")
    archive = tmp_path / "root.tar"
    with tarfile.open(archive, "w") as out:
        out.add(root, arcname=".")
    expected = _rootfs.manifest(archive)
    assert _btrfs_worker.inventory(root, _rootfs.SCOPE, include_root=True) == expected


def test_missing_external_snapshot_invalidates_node_and_bundle_is_refused(
    tmp_path, snapshot, monkeypatch
):
    class Pool:
        missing = False

        def verify(self, selected):
            assert selected == snapshot
            if self.missing:
                raise FileNotFoundError("snapshot was deleted")

    pool = Pool()
    key = (snapshot.location.daemon_id, snapshot.location.volume)
    monkeypatch.setitem(btrfs_pool._ACTIVE, key, pool)
    path = tmp_path / "record"
    with RecordStore.create(path) as store:
        write_json(path / "btrfs.json", snapshot.location.model_dump(mode="json"))

        def capture(store, boundary, previous):
            manifest = store.put_json(snapshot.model_dump(mode="json"))
            return State(
                backend=btrfs_models.BACKEND,
                capture=capture_info(),
                manifest=manifest,
                runtime=store.put_json({}),
                artifacts=(manifest,),
            )

        with Recorder(
            store,
            task_id="fixture",
            agent_name="fixture",
            agent_version="1",
            config=Payload.json({}),
            boundary=BoundaryAdapter(
                lambda boundary: AgentSnapshot(Payload.json({}), Payload.json({})),
                capture,
            ),
        ) as recorder:
            node = recorder.current_node
            recorder.finish()
        assert store.get_node(node.node_id) == node
        pool.missing = True
        with pytest.raises(IntegrityError, match="Btrfs material"):
            store.get_node(node.node_id)
        assert not store.check_integrity().ok
        pool.missing = False
    with pytest.raises(RecordUsageError, match="portable bundles"):
        export_record(path, tmp_path / "bundle", bundle=True)
    assert not (tmp_path / "bundle").exists()


def test_failed_snapshot_unpauses_container_and_returns_no_state():
    calls = []

    def fail(*args, **kwargs):
        raise IntegrityError("injected storage failure")

    sandbox = SimpleNamespace(
        pool=SimpleNamespace(snapshot=fail),
        workspace="workspaces/" + uuid4().hex,
        _container=SimpleNamespace(
            pause=lambda: calls.append("pause"), unpause=lambda: calls.append("unpause")
        ),
        check_root=lambda: None,
    )
    backend = record_state.BtrfsStateBackend(sandbox, public_env_keys=())
    with pytest.raises(IntegrityError, match="injected storage failure"):
        backend.capture(None, SimpleNamespace(index=1, node_id=uuid4().hex), None)
    assert calls == ["pause", "unpause"]


def test_backend_saves_state_before_capture_evidence(tmp_path, snapshot, monkeypatch):
    launch = C0LaunchConfig(
        platform="linux/amd64",
        environment={"PATH": "/bin"},
        omitted_environment_keys=(),
        workdir="/",
        user="",
        shell=("bash", "-c"),
        entrypoint=("sleep",),
        command=("infinity",),
        network_mode="none",
        memory_bytes=0,
        nano_cpus=0,
    )
    pool = SimpleNamespace(verify=lambda selected: None, command=lambda *args, **kwargs: {})
    monkeypatch.setitem(
        btrfs_pool._ACTIVE, (snapshot.location.daemon_id, snapshot.location.volume), pool
    )
    metadata = SimpleNamespace(
        snapshot=snapshot,
        launch=launch,
        source_image_id="sha256:" + "b" * 64,
        bootstrap_image_id="sha256:" + "c" * 64,
        oracle_sha256="d" * 64,
    )
    sandbox = SimpleNamespace(
        pool=pool,
        check_root=lambda: None,
        _network="none",
        preload=SimpleNamespace(base=SimpleNamespace(metadata=metadata)),
        _container=SimpleNamespace(
            id="e" * 64, reload=lambda: None, attrs={"Config": {"Env": ["PATH=/bin"]}}
        ),
    )
    backend = record_state.BtrfsStateBackend(sandbox, public_env_keys=("PATH",))
    with RecordStore.create(tmp_path / "record") as store:
        with Recorder(
            store,
            task_id="fixture",
            agent_name="fixture",
            agent_version="1",
            config=Payload.json({}),
            boundary=BoundaryAdapter(
                lambda boundary: AgentSnapshot(Payload.json({}), Payload.json({})),
                backend.capture,
            ),
        ) as recorder:
            assert len(store.list(State)) == len(store.list(StateCapture)) == 1
            assert store.list(StateCapture)[0].state_id == recorder.current_node.state_id
            recorder.finish()


def test_preload_checksum_is_checked_before_any_docker_use(tmp_path):
    raw = b"{}\n"
    (tmp_path / "c0.json").write_bytes(raw)
    (tmp_path / "c0.checksum.json").write_text(json.dumps({"sha256": "0" * 64}))
    with pytest.raises(IntegrityError, match="checksum mismatch"):
        preload_base.open_preload(tmp_path)
