"""Offline restore orchestration using real workspaces and filesystem comparison.

Directory copies replace Btrfs snapshot syscalls here; actual CoW isolation still
requires a Linux Docker/Btrfs acceptance run.
"""

import json
import shutil
from types import SimpleNamespace
from uuid import uuid4

import pytest

import palinreplay.record.restore as restore
import palinreplay.storage.btrfs.pool as pool_module
import palinreplay.storage.btrfs.verification as verification
import palinreplay.storage.btrfs.worker as worker
from palinreplay.cli import main
from palinreplay.core.models import State
from palinreplay.sandbox.base import SandboxError
from palinreplay.storage.btrfs.models import Snapshot
from palinreplay.storage.store import IntegrityError, RecordStore
from tests.record.test_restore_materials import _record_run, _rewrite_record


class MissingResource(Exception):
    status_code = 404


@pytest.fixture
def environment(tmp_path, monkeypatch):
    path = tmp_path / "record"
    with monkeypatch.context() as recording:
        recording.setattr(verification, "verify_state", lambda store, state: None)
        with RecordStore.create(path) as store:
            run, c0, c1, _, _ = _record_run(store, "one")
            snapshots = {
                node.index: Snapshot.model_validate(
                    store.read_json(store.get(State, node.state_id).manifest)
                )
                for node in (c0, c1)
            }

    root = tmp_path / "pool"
    identities = {}
    for index, snapshot in snapshots.items():
        source = root / snapshot.name
        source.mkdir(parents=True)
        (source / "file").write_text(f"saved C{index}")
        identities[snapshot.name] = {"uuid": str(snapshot.uuid), "readonly": True}

    state = SimpleNamespace(
        path=path, root=root, run=run, c0=c0, c1=c1, snapshots=snapshots,
        opened=[], commands=[], volumes={}, containers={}, created=[], closed=[],
        bad_identity=False, fail_start=False, change_on_start=False, client_closed=False,
    )

    def lookup(resources, name):
        if name not in resources:
            raise MissingResource(name)
        return resources[name]

    def volume_create(name, **options):
        volume = SimpleNamespace(
            attrs={"Labels": options["labels"]},
            remove=lambda: state.volumes.pop(name),
        )
        state.volumes[name] = volume
        return volume

    def container_create(**options):
        state.created.append(options)
        name = options["name"]

        def start():
            if state.fail_start:
                raise RuntimeError("injected container start failure")
            if state.change_on_start:
                (root / ("workspaces/" + name.removeprefix("pr-btrfs-")) / "file").write_text(
                    "different file content"
                )

        def remove(**kwargs):
            state.containers.pop(name)

        container = SimpleNamespace(
            id=uuid4().hex,
            attrs={"Config": {"Labels": options["labels"]}, "State": {"Running": True}},
            start=start, reload=lambda: None, remove=remove,
            exec_run=lambda *args, **kwargs: SimpleNamespace(exit_code=0, output=b"btrfs\n"),
        )
        state.containers[name] = container
        return container

    def close_client():
        state.client_closed = True

    client = SimpleNamespace(
        images=SimpleNamespace(get=lambda image: SimpleNamespace(
            id=image, attrs={"Os": "linux", "Architecture": "amd64"}
        )),
        volumes=SimpleNamespace(
            create=volume_create, get=lambda name: lookup(state.volumes, name)
        ),
        containers=SimpleNamespace(
            create=container_create, get=lambda name: lookup(state.containers, name)
        ),
        close=close_client,
    )
    state.client = client

    class Pool(pool_module.BtrfsPool):
        def __enter__(self):
            assert self.key not in pool_module._ACTIVE
            state.opened.append(self)
            self.device = "/dev/fixture"
            monkeypatch.setitem(pool_module._ACTIVE, self.key, self)
            return self

        def command(self, op, **values):
            state.commands.append((op, values))
            if op == "info":
                if state.bad_identity:
                    return {"uuid": str(uuid4()), "readonly": True}
                return identities[values["name"]]
            if op == "snapshot":
                destination = root / values["destination"]
                destination.parent.mkdir(exist_ok=True)
                shutil.copytree(root / values["source"], destination)
                result = {"uuid": str(uuid4()), "readonly": values["readonly"]}
                identities[values["destination"]] = result
                return result
            if op == "verify":
                try:
                    return worker.operation({"op": op, **values})
                except ValueError as error:
                    raise IntegrityError(f"Btrfs operation failed: {error}") from error
            if op == "delete":
                shutil.rmtree(root / values["name"])
                identities.pop(values["name"])
                return {"deleted": True}
            pytest.fail(f"unexpected pool operation: {op}")

        def close(self):
            state.closed.append(self)
            pool_module._ACTIVE.pop(self.key, None)
            self.closed = True

    monkeypatch.setattr(restore, "BtrfsPool", Pool)
    monkeypatch.setattr(worker, "POOL", root)
    monkeypatch.setattr(worker, "healthy", lambda: None)
    return state


@pytest.mark.parametrize("selector", ["C0", "C1", "full-id"])
def test_light_restore_uses_selected_snapshot_without_scanning(environment, monkeypatch, selector):
    env = environment
    index = 0 if selector == "C0" else 1
    if selector == "full-id":
        selector = env.c1.node_id

    def no_scan(*args, **kwargs):
        pytest.fail("light restore attempted a full scan or history audit")

    monkeypatch.setattr(worker, "inventory", no_scan)
    monkeypatch.setattr(RecordStore, "get_node", no_scan)
    report = env.path.parent / "light.json"
    with RecordStore.open(env.path) as store:
        with restore.restore_r1_node(
            store, selector, run_id=env.run.run_id, client=env.client,
            docker_host="unix:///fixture.sock", report_path=report,
        ) as restored:
            assert restored.workspace.snapshot == env.snapshots[index]
            assert (env.root / restored.workspace.name / "file").read_text() == f"saved C{index}"
            assert restored.container is restored.workspace.container
            assert restored.report["status"] == "ready"
            assert restored.report["verification"] == "snapshot-identity"
            assert "file_comparison" not in restored.report
            assert "material_verified" not in restored.report
            assert len(env.opened) == 1
            options = env.created[0]
            assert options["image"] == "sha256:" + "d" * 64
            assert options["working_dir"] == "/" and options["environment"] == {"PATH": "/bin"}
            assert options["network_mode"] == "none"
            assert options["stdin_open"] is False and options["detach"] is True
        assert store.get(State, restored.state.state_id) == restored.state
    snapshots = [values for op, values in env.commands if op == "snapshot"]
    assert snapshots == [{
        "source": env.snapshots[index].name,
        "destination": restored.workspace.name,
        "readonly": False,
    }]
    assert not any(op in ("verify", "import") for op, _ in env.commands)
    assert env.closed == env.opened and not env.volumes and not env.containers
    assert not env.client_closed
    assert json.loads(report.read_text())["cleanup_confirmed"]
    with pytest.raises(SandboxError, match="not available"):
        restored.__enter__()


@pytest.mark.parametrize("selected", [0, 1])
def test_restored_context_delivers_selected_messages_config_counters_and_position(
    environment, selected
):
    env = environment
    expected = {}
    with RecordStore.open(env.path, read_only=False) as store:
        for index, node in enumerate((env.c0, env.c1)):
            context = {
                "messages": [{"role": "user", "content": f"at C{index}"}],
                "agent_config": {"budget": 7 + index},
                "model_config": {"model_name": "fixture"},
                "counters": {"n_calls": index, "cost": index * 0.25},
            }
            position = {"next_message": index + 1, "event_sequence": node.accepted_event_sequence}
            expected[index] = (context, position)
            _rewrite_record(env.path, node.model_copy(update={
                "context": store.put_json(context), "input_position": store.put_json(position),
            }))
    source = env.c0 if selected == 0 else env.c1
    with RecordStore.open(env.path) as store:
        with restore.restore_r1_node(
            store, source.node_id, client=env.client, docker_host="unix:///fixture.sock",
            report_path=env.path.parent / "context.json",
        ) as restored:
            assert restored.run == env.run and restored.node.node_id == source.node_id
            assert restored.config == store.read_json(env.run.config)
            assert restored.context == expected[selected][0]
            assert restored.input_position == expected[selected][1]
            assert restored.workspace is not None and restored.container is not None
            restored.context["messages"][0]["content"] = "caller changed"
            restored.context["counters"]["n_calls"] = 99
            restored.config["name"] = "caller changed"
            restored.input_position["next_message"] = 99
            assert store.read_json(restored.node.context) == expected[selected][0]
            assert store.read_json(restored.node.input_position) == expected[selected][1]
            assert store.read_json(env.run.config)["name"] == "one"
    assert restored.context["messages"][0]["content"] == "caller changed"


def test_independent_workspaces_keep_the_source_and_fresh_restore(environment):
    env = environment
    names = []
    with RecordStore.open(env.path) as store:
        for index in range(2):
            with restore.restore_r1_node(
                store, "C1", client=env.client, docker_host="unix:///fixture.sock",
                report_path=env.path.parent / f"copy-{index}.json",
            ) as restored:
                names.append(restored.workspace.name)
                copied = env.root / restored.workspace.name / "file"
                assert copied.read_text() == "saved C1"
                copied.write_text("local modification")
                assert (env.root / env.snapshots[1].name / "file").read_text() == "saved C1"
    assert names[0] != names[1]
    assert len(env.opened) == len(env.closed) == 2


@pytest.mark.parametrize("difference", [False, True])
def test_full_restore_still_compares_real_file_contents(environment, difference):
    env = environment
    env.change_on_start = difference
    report = env.path.parent / "verified.json"
    with RecordStore.open(env.path) as store:
        if difference:
            with pytest.raises(IntegrityError, match="restored filesystem differs") as failure:
                restore.restore_node(
                    store, env.c1.node_id, client=env.client, docker_host="unix:///fixture.sock",
                    report_path=report,
                )
            assert failure.value.restore_report["status"] == "failed"
        else:
            with restore.restore_node(
                store, env.c1.node_id, client=env.client, docker_host="unix:///fixture.sock",
                report_path=report,
            ) as restored:
                assert restored.report["status"] == "verified"
                assert restored.report["verification"] == "full-logical-filesystem"
                assert restored.report["file_comparison"]["equal"]
    assert len(env.opened) == len(env.closed) == 1
    assert [op for op, _ in env.commands].count("verify") == 1
    assert not env.volumes and not env.containers
    assert json.loads(report.read_text())["cleanup_confirmed"]


@pytest.mark.parametrize("failure", ["identity", "start"])
def test_light_restore_failure_cleans_owned_resources(environment, failure):
    env = environment
    env.bad_identity = failure == "identity"
    env.fail_start = failure == "start"
    report = env.path.parent / "failed.json"
    expected = IntegrityError if failure == "identity" else RuntimeError
    with RecordStore.open(env.path) as store:
        with pytest.raises(expected) as error:
            restore.restore_r1_node(
                store, "C1", client=env.client, docker_host="unix:///fixture.sock",
                report_path=report,
            )
        assert error.value.restore_report["status"] == "failed"
        assert not store._closed
    assert len(env.opened) == len(env.closed) == 1
    assert not env.volumes and not env.containers
    assert not list((env.root / "workspaces").glob("*"))
    assert json.loads(report.read_text())["cleanup_confirmed"]
    assert not env.client_closed


def test_exception_in_restore_body_cleans_resources_without_closing_borrowed_handles(environment):
    env = environment
    failure = RuntimeError("caller failed")
    report = env.path.parent / "body-failed.json"
    with RecordStore.open(env.path) as store:
        with pytest.raises(RuntimeError) as error:
            with restore.restore_r1_node(
                store, "C1", client=env.client, docker_host="unix:///fixture.sock",
                report_path=report,
            ):
                raise failure
        assert error.value is failure
        assert store.get(State, env.c0.state_id).state_id == env.c0.state_id
    assert len(env.closed) == 1 and not env.volumes and not env.containers
    assert not env.client_closed
    assert (env.root / env.snapshots[1].name / "file").read_text() == "saved C1"
    assert json.loads(report.read_text())["cleanup_confirmed"]


@pytest.mark.parametrize("during_start", [False, True])
def test_cleanup_failures_preserve_primary_error_and_attempt_both_resources(
    environment, monkeypatch, during_start
):
    env = environment
    env.fail_start = during_start
    calls = []
    workspace_close = restore.BtrfsWorkspace.close
    pool_close = restore.BtrfsPool.close

    def close_workspace(workspace):
        calls.append("workspace")
        workspace_close(workspace)
        raise OSError("workspace cleanup confirmation failed")

    def close_pool(pool):
        calls.append("pool")
        pool_close(pool)
        raise OSError("pool cleanup confirmation failed")

    monkeypatch.setattr(restore.BtrfsWorkspace, "close", close_workspace)
    monkeypatch.setattr(restore.BtrfsPool, "close", close_pool)
    report = env.path.parent / "cleanup-failed.json"
    with RecordStore.open(env.path) as store:
        with pytest.raises(RuntimeError, match="start failure" if during_start else "caller failed") as error:
            with restore.restore_r1_node(
                store, "C1", client=env.client, docker_host="unix:///fixture.sock",
                report_path=report,
            ):
                raise RuntimeError("caller failed")
        assert not store._closed
    assert calls == ["workspace", "pool"]
    assert any("cleanup" in note for note in error.value.__notes__)
    saved = json.loads(report.read_text())
    assert not saved["cleanup_confirmed"] and len(saved["cleanup_errors"]) == 2
    assert not env.volumes and not env.containers and not env.client_closed
    assert (env.root / env.snapshots[1].name / "file").read_text() == "saved C1"


@pytest.mark.parametrize("difference", [False, True])
def test_verify_restore_command_opens_one_pool_and_detects_differences(
    environment, monkeypatch, capsys, difference
):
    import palinreplay.sandbox.client as connection

    env = environment
    env.change_on_start = difference
    monkeypatch.setattr(connection, "docker_endpoint", lambda: "unix:///fixture.sock")
    monkeypatch.setattr(connection, "connect_docker", lambda _: env.client)
    report = env.path.parent / "cli.json"
    assert main([
        "record", "verify-restore", str(env.path), "--node", "C1", "--report", str(report)
    ]) == (1 if difference else 0)
    assert len(env.opened) == len(env.closed) == 1
    assert [op for op, _ in env.commands].count("verify") == 1
    assert env.client_closed
    assert ("File restoration verified" in capsys.readouterr().out) is not difference
