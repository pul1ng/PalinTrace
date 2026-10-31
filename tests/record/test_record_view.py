import base64
import json
import shutil
import sqlite3
import subprocess
import sys

import pytest

from palinreplay.cli import main
from palinreplay.core.models import Run, State
from palinreplay.record.inspect import export_record, inspect_record
from palinreplay.storage.store import IntegrityError, RecordStore
from tests.record.test_recorder import fake
from tests.storage.test_store import begin_step, call, start


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "history"
    fake.main(root)
    return root


def test_inspect_closed_record_shows_nodes_calls_and_actual_adoption(history, capsys):
    assert main(["record", "inspect", str(history), "--check", "--node", "C2"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["verification"]["ok"]
    run = view["runs"][0]
    assert run["record_complete"] and run["task_success"] == "not_evaluated"
    assert [n["label"] for n in run["nodes"]] == ["C0", "C1", "C2", "C3"]
    node = view["selected_node"]
    assert node["input_position_content"]["value"]["next_message"] == 7
    error_call = node["calls"][-1]
    assert error_call["phase"] == "accepted"
    assert error_call["events"][1]["outcome"] == "error"
    assert error_call["events"][1]["content"]["value"]["exit_code"] == 2
    assert error_call["events"][-1]["content"]["value"]["adopted_by"] == "step4-fixture-agent"
    assert (
        main(
            [
                "record",
                "inspect",
                str(history),
                "--execution",
                error_call["execution"]["execution_id"],
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["selected_execution"] == error_call


def test_default_export_output_uses_record_task(history, tmp_path, monkeypatch):
    import palinreplay.cli.outputs as outputs

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(outputs, "timestamp", lambda: "261031_131022")
    with RecordStore.open(history / "record") as store:
        task_id = store.list(Run)[0].task_id
    assert main(["record", "export", str(history)]) == 0
    path = tmp_path / "outputs/export" / f"261031_131022_{task_id}"
    assert (path / "trajectory.json").is_file()


@pytest.mark.parametrize("custom", [False, True])
def test_restore_report_default_and_output_alias(history, tmp_path, monkeypatch, custom):
    from contextlib import contextmanager
    from types import SimpleNamespace

    import palinreplay.cli.outputs as outputs
    import palinreplay.record.restore as backend
    import palinreplay.sandbox.client as connection

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(outputs, "timestamp", lambda: "261031_131022")
    monkeypatch.setattr(connection, "docker_endpoint", lambda: "unix:///fixture")
    monkeypatch.setattr(connection, "connect_docker", lambda _: SimpleNamespace(close=lambda: None))

    @contextmanager
    def restore(store, node_id, *, report_path, **kwargs):
        report_path.write_text(json.dumps({"ok": True, "node": node_id}))
        yield

    monkeypatch.setattr(backend, "restore_node", restore)
    args = ["record", "verify-restore", str(history), "--node", "C0"]
    with RecordStore.open(history / "record") as store:
        task_id = store.list(Run)[0].task_id
    path = tmp_path / "outputs/verify-restore" / f"261031_131022_{task_id}_C0.json"
    if custom:
        path = tmp_path / "custom.json"
        args += ["--output", str(path)]
    assert main(args) == 0
    assert json.loads(path.read_text())["ok"]
    if custom:
        assert not (tmp_path / "outputs").exists()


def test_unchecked_summary_and_failed_tail_are_not_success(history, capsys):
    assert main(["record", "inspect", str(history)]) == 0
    assert "Integrity: not checked" in capsys.readouterr().out
    view = inspect_record(history / "unknown", check=True)
    assert view["verification"]["ok"]
    run = view["runs"][0]
    assert run["status"] == "failed" and not run["record_complete"]
    assert len(run["nodes"]) == 1 and len(run["incomplete_steps"]) == 1
    assert len(run["unknown_executions"]) == 1
    assert run["failures"]


@pytest.mark.parametrize("bundle", [False, True])
def test_lossless_export_and_reopen_after_source_removal(tmp_path, history, bundle):
    source = history / "unknown"
    output = tmp_path / "export"
    before = inspect_record(source, check=True)
    manifest = export_record(source, output, bundle=bundle)
    assert manifest["source_integrity_ok"] and manifest["incomplete_steps"]
    document = json.loads((output / "trajectory.json").read_text())
    assert document["restorable"] is False
    assert document["runs"][0]["incomplete_steps"] == before["runs"][0]["incomplete_steps"]
    assert not document["runs"][0]["record_complete"]
    if bundle:
        with RecordStore.open(source) as store:
            ref = store.list(State)[0].artifacts[0]
        relative = f"artifacts/{ref.sha256[:2]}/{ref.sha256[2:]}"
        assert (source / relative).stat().st_ino != (output / "record" / relative).stat().st_ino
        shutil.rmtree(history)
        restored = inspect_record(output / "record", check=True)
        assert restored["verification"]["ok"]
        assert restored["runs"] == before["runs"]
    else:
        assert not (output / "record").exists()
        assert manifest["artifact_bytes_copied"] == 0


def test_binary_results_are_base64_not_replacement_decoded(tmp_path):
    source = tmp_path / "record"
    with RecordStore.create(source) as store:
        run, attempt, _, node = start(store)
        step = begin_step(store, run, attempt, node)
        execution = call(store, step, stages=2, outcome="unknown")
    view = inspect_record(source, execution=execution.execution_id)
    raw = view["selected_execution"]["events"][1]["content"]
    assert raw["encoding"] == "base64"
    assert base64.b64decode(raw["value"]) == b"raw\x00\xff\r\n"
    assert view["runs"][0]["status"] == "unfinalized"


@pytest.mark.parametrize("damage", ["missing", "corrupt", "symlink", "catalog"])
def test_corrupt_store_inspection_fails_and_export_is_not_published(
    tmp_path, history, damage, capsys
):
    source = history / "record"
    if damage == "catalog":
        with sqlite3.connect(source / "catalog.sqlite") as db:
            db.execute("UPDATE records SET body='{}' WHERE kind='node'")
    else:
        with RecordStore.open(source) as store:
            ref = store.list(State)[0].artifacts[0]
        path = source / "artifacts" / ref.sha256[:2] / ref.sha256[2:]
        path.unlink()
        if damage == "corrupt":
            path.write_bytes(b"wrong bytes")
        elif damage == "symlink":
            target = tmp_path / "external"
            target.write_bytes(b"wrong bytes")
            path.symlink_to(target)
    assert main(["record", "inspect", str(source), "--check", "--json"]) == 1
    view = json.loads(capsys.readouterr().out)
    assert not view["verification"]["ok"] and view["verification"]["issues"]
    output = tmp_path / "export"
    with pytest.raises(IntegrityError):
        export_record(source, output, bundle=True)
    assert not output.exists()
    assert not list(tmp_path.glob(".record-export-*"))


def test_export_copy_failure_keeps_source_and_removes_partial_output(
    tmp_path, history, monkeypatch
):
    original = RecordStore.export_artifact
    count = 0

    def fail(self, ref, destination):
        nonlocal count
        count += 1
        if count == 3:
            raise OSError("disk full")
        original(self, ref, destination)

    monkeypatch.setattr(RecordStore, "export_artifact", fail)
    with pytest.raises(OSError):
        export_record(history / "record", tmp_path / "export", bundle=True)
    assert not (tmp_path / "export").exists()
    assert inspect_record(history / "record", check=True)["verification"]["ok"]


def test_export_rejects_existing_and_nested_output(tmp_path, history):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep").write_text("user data")
    with pytest.raises(FileExistsError):
        export_record(history / "record", output)
    assert (output / "keep").read_text() == "user data"
    with pytest.raises(ValueError, match="outside"):
        export_record(history / "record", history / "record" / "export")
    link = tmp_path / "alias"
    link.symlink_to(history / "record", target_is_directory=True)
    with pytest.raises(ValueError):
        export_record(history / "record", link / "export")


def test_export_snapshot_excludes_later_writes_and_omits_orphan_bytes(tmp_path, monkeypatch):
    source, output = tmp_path / "record", tmp_path / "export"
    with RecordStore.create(source) as writer:
        writer._db.execute("PRAGMA journal_mode=WAL")
        run, attempt, _, c0 = start(writer)
        writer.put_bytes(b"orphan not referenced")
        (source / "artifacts/.tmp/interrupted").write_bytes(b"partial")
        original = RecordStore.export_catalog

        def backup_then_write(self, destination):
            original(self, destination)
            begin_step(writer, run, attempt, c0)

        monkeypatch.setattr(RecordStore, "export_catalog", backup_then_write)
        report = export_record(source, output, bundle=True)
        assert report["omitted_staged_artifacts"] == ["interrupted"]
        with RecordStore.open(output / "record") as clone:
            assert not clone.check_integrity().incomplete_steps
            assert clone.check_integrity().ok
        assert writer.check_integrity().incomplete_steps


def test_node_selection_multi_run_and_no_sdk_imports(tmp_path):
    with RecordStore.create(tmp_path / "record") as store:
        first = start(store, name="one")[-1]
        start(store, name="two")
    with pytest.raises(ValueError, match="exactly one"):
        inspect_record(tmp_path / "record", node="C0")
    assert (
        inspect_record(tmp_path / "record", node="C0", run_id="one")["selected_node"]["node_id"]
        == first.node_id
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import palinreplay.cli; "
            "assert 'minisweagent' not in sys.modules; "
            "assert 'docker' not in sys.modules",
        ],
        capture_output=True,
    )
    assert result.returncode == 0


def test_cli_export_and_bad_selectors(history, tmp_path, capsys):
    assert main(["record", "export", str(history), "--output", str(tmp_path / "export")]) == 0
    assert "Exported trajectory" in capsys.readouterr().out
    assert main(["record", "inspect", str(history), "--node", "C999"]) == 1
    assert "error:" in capsys.readouterr().err
    with pytest.raises(SystemExit) as result:
        main(["record", "inspect", str(history), "--node", "C0", "--execution", "anything"])
    assert result.value.code == 2


def test_declared_but_uninvoked_tool_group_is_visible(tmp_path):
    source = tmp_path / "record"
    with RecordStore.create(source) as store:
        agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
        rec = fake.recorder(store, agent, backend)
        rec.start()
        fake.answer(rec, agent, ("never-started",))
        rec.end_incomplete("native short circuit")
        rec.close()
    view = inspect_record(source, check=True)
    step = view["runs"][0]["steps"][0]
    assert not step["committed"]
    assert step["unprepared_tool_call_ids"] == ["never-started"]


def test_restore_command_validates_before_docker_and_preserves_failure(
    history, tmp_path, monkeypatch, capsys
):
    from contextlib import contextmanager
    from types import SimpleNamespace

    import palinreplay.record.restore as backend
    import palinreplay.sandbox.client as connection

    invoked = []
    closed = []
    client = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(connection, "connect_docker", lambda _: client)
    monkeypatch.setattr(connection, "docker_endpoint", lambda: "unix:///fake")

    @contextmanager
    def restore(store, node_id, **kwargs):
        invoked.append(node_id)
        raise RuntimeError("injected restore comparison failure")
        yield  # pragma: no cover

    monkeypatch.setattr(backend, "restore_node", restore)
    assert (
        main(
            [
                "record",
                "verify-restore",
                str(history),
                "--node",
                "C99",
                "--report",
                str(tmp_path / "bad.json"),
            ]
        )
        == 1
    )
    assert not invoked and not closed
    assert (
        main(
            [
                "record",
                "verify-restore",
                str(history),
                "--node",
                "C0",
                "--report",
                str(tmp_path / "restore.json"),
            ]
        )
        == 1
    )
    assert len(invoked) == 1 and closed == [True]
    assert "restoration verified" not in capsys.readouterr().out
    assert inspect_record(history / "record", check=True)["verification"]["ok"]
