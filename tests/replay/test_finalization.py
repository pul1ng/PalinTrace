import json

import pytest

import palinreplay.replay.session as session
from palinreplay.cli import main
from palinreplay.core.models import Failure, Node, RunEnd
from palinreplay.record.inspect import export_record, inspect_record
from palinreplay.storage import io
from palinreplay.storage.replay_finalization import envelope
from palinreplay.storage.store import RecordStore
from tests.replay.test_session import deterministic  # noqa: F401
from tests.replay.test_session import replay_session as replay_session


def replay_view(output):
    view = inspect_record(output / "record", check=True)
    assert view["verification"]["ok"], view["verification"]
    return view["runs"][0]["replay"]


@pytest.mark.parametrize("diagnostic_fault", [False, True])
def test_cancellation_and_result_io_failure_agree(tmp_path, replay_session, monkeypatch, diagnostic_fault):
    state = replay_session
    save, append = RecordStore.save, RecordStore.append_event
    def broken(store, fact):
        if store.root.name == "record" and diagnostic_fault and isinstance(fact, Failure):
            raise OSError("diagnostic disk fault")
        return save(store, fact)
    def broken_event(store, execution_id, kind, **kwargs):
        if store.root.name == "record" and kind == "result" and kwargs.get("outcome") == "unknown":
            raise OSError("result disk fault")
        return append(store, execution_id, kind, **kwargs)
    monkeypatch.setattr(RecordStore, "save", broken)
    monkeypatch.setattr(RecordStore, "append_event", broken_event)
    def cancelled(request):
        raise KeyboardInterrupt("backend cancellation")
    state.executor.execute = cancelled
    output = tmp_path / "output"
    events = []
    report = session.run_replay(state.source, "C0", "C3", output=output, progress=events.append)
    assert report["execution_status"] == "failed"
    assert report["cancellation"]["error"] == "backend cancellation"
    assert "result disk fault" in str(report["outcome_evidence"])
    assert [e.status for e in events if e.phase == "finished"] == ["failed"]
    view = replay_view(output)
    assert view["execution_status"] == "failed"
    assert view["finalization"]["status"] == "confirmed"
    assert view["outcome_evidence"]["cancellations"]
    with RecordStore.open(output / "record") as store:
        assert len(store.list(RunEnd)) == 1
        assert store.list(RunEnd)[0].status == "cancelled"
        assert not store.list(Node)
    # Exercise the real CLI mapping, with the already verified report returned.
    monkeypatch.setattr(session, "run_replay", lambda *a, **k: report)
    assert main(["replay", str(state.source), "--from", "C0", "--output", str(tmp_path / "cli")]) == 1


@pytest.mark.parametrize("fault", ["r1", "report", "close-before", "close-after", "late-report"])
def test_finalization_faults_survive_store_close(tmp_path, replay_session, monkeypatch, fault):
    state = replay_session
    write, close = session.write_json, RecordStore.close
    writes = 0
    def broken_write(path, value, **kwargs):
        nonlocal writes
        if path.name == "report.json" and kwargs.get("replace"):
            writes += 1
            if fault == "report" or (fault == "late-report" and writes == 2):
                raise OSError("report publication fault")
        if fault == "r1" and path.name == "r1-validation.json":
            raise OSError("r1 publication fault")
        return write(path, value, **kwargs)
    def broken_close(store):
        if store.root.name == "record" and not store.read_only and fault.startswith("close-"):
            if fault == "close-after":
                close(store)
            raise OSError("Store close fault")
        return close(store)
    monkeypatch.setattr(session, "write_json", broken_write)
    monkeypatch.setattr(RecordStore, "close", broken_close)
    output = tmp_path / "output"
    report = session.run_replay(state.source, "C0", "C3", output=output)
    assert report["execution_status"] == "failed"
    view = replay_view(output)
    assert view["execution_status"] == "failed" and view["completed_steps"] == 3
    receipt = view["finalization"]["receipt"]
    assert receipt["faults"]
    assert receipt["execution_status"] == "failed"
    with RecordStore.open(output / "record") as store:
        assert len(store.list(RunEnd)) == 1 and store.list(RunEnd)[0].status == "completed"
        assert len(store.list(Node)) == 3
        if fault in ("r1", "report", "close-before"):
            assert any(f.category == "cleanup" for f in store.list(Failure))
    assert state.recorder.store._closed
    if fault != "report":
        assert json.loads((output / "report.json").read_text())["execution_status"] == "failed"


def test_dispatch_before_executor_entry_has_unknown_result(tmp_path, replay_session, monkeypatch):
    state = replay_session
    append = RecordStore.append_event
    def interrupt_after_dispatch(store, execution_id, kind, **kwargs):
        result = append(store, execution_id, kind, **kwargs)
        if store.root.name == "record" and kind == "dispatched":
            from palinreplay.core.models import Execution, Operation
            execution = store.get(Execution, execution_id)
            if store.get(Operation, execution.operation_id).kind == "tool":
                raise KeyboardInterrupt("before executor entry")
        return result
    monkeypatch.setattr(RecordStore, "append_event", interrupt_after_dispatch)
    output = tmp_path / "output"
    with pytest.raises(KeyboardInterrupt):
        session.run_replay(state.source, "C0", "C3", output=output)
    report = json.loads((output / "report.json").read_text())
    assert report["execution_status"] == "cancelled"
    assert state.sandbox.calls == []
    assert report["statistics"]["actual_tool_executions"] == 0
    usage = replay_view(output)["history_usage"]
    assert usage["dispatched_tool_calls"] == 1
    assert usage["unknown_result_execution_ids"] == usage["dispatched_tool_execution_ids"]
    assert "actual_tool_executions" not in usage


@pytest.mark.parametrize("damage", ["absent", "bytes", "binding", "checksum", "export", "scope"])
def test_receipt_is_required_bound_and_exported(tmp_path, replay_session, damage):
    output = tmp_path / "output"
    report = session.run_replay(replay_session.source, "C0", "C3", output=output)
    assert report["execution_status"] == "completed"
    view = replay_view(output)
    assert view["finalization"]["status"] == "confirmed"
    path = output / report["finalization"]["receipt"]
    if damage == "absent":
        path.unlink()
        view = replay_view(output)
        assert view["execution_status"] == "unconfirmed"
        assert view["finalization"]["status"] == "unconfirmed"
        export_record(output / "record", tmp_path / "export")
        trajectory = json.loads((tmp_path / "export" / "trajectory.json").read_text())
        assert trajectory["runs"][0]["replay"] == view
    elif damage == "export":
        export_record(output / "record", tmp_path / "export")
        trajectory = json.loads((tmp_path / "export" / "trajectory.json").read_text())
        assert trajectory["runs"][0]["replay"] == view
    elif damage == "scope":
        with RecordStore.open(output / "record") as store:
            with store.snapshot():
                assert store.replay_finalization(report["run_id"]) == view["finalization"]
                path.write_bytes(b"changed after scope read")
                assert store.replay_finalization(report["run_id"]) == view["finalization"]
            assert not store.check_integrity().ok
    else:
        document = json.loads(path.read_bytes())
        if damage == "bytes":
            path.write_bytes(b"broken")
        elif damage == "binding":
            document["payload"]["run_id"] = "wrong-run"
            path.write_text(json.dumps(envelope(document["payload"])))
        else:
            document["sha256"] = "0" * 64
            path.write_text(json.dumps(document))
        checked = inspect_record(output / "record", check=True)
        assert not checked["verification"]["ok"]
        assert "receipt" in str(checked["verification"]["issues"])


@pytest.mark.parametrize("phase", ["link", "link-after", "directory-sync", "sync-after", "stage-cleanup"])
def test_receipt_publication_does_not_confuse_visibility_and_durability(tmp_path, monkeypatch, phase):
    path = tmp_path / "receipt.json"
    if phase == "link":
        monkeypatch.setattr(io.os, "link", lambda *a: (_ for _ in ()).throw(OSError("link fault")))
    elif phase == "directory-sync":
        monkeypatch.setattr(io, "sync_directory", lambda *a: (_ for _ in ()).throw(OSError("sync fault")))
    elif phase == "link-after":
        link = io.os.link
        def after_link(*args):
            link(*args)
            raise OSError("after atomic publication")
        monkeypatch.setattr(io.os, "link", after_link)
    elif phase == "sync-after":
        sync = io.sync_directory
        def after_sync(*args):
            sync(*args)
            raise OSError("after sync acknowledgement")
        monkeypatch.setattr(io, "sync_directory", after_sync)
    else:
        from pathlib import Path
        unlink = Path.unlink
        def broken_unlink(p, **kwargs):
            if p.name.startswith(".receipt-"):
                raise OSError("stage cleanup fault")
            return unlink(p, **kwargs)
        monkeypatch.setattr(Path, "unlink", broken_unlink)
    result = io.publish_receipt_json(path, {"ok": True})
    assert result["status"] == "unconfirmed" and not result["directory_synced"]
    assert not path.exists() and not result["published"]


def test_receipt_cannot_upgrade_an_endpoint_exit_mismatch_to_completed(tmp_path, replay_session):
    replay_session.different_call = 1
    replay_session.difference_kind = "exit-code"
    output = tmp_path / "output"
    report = session.run_replay(replay_session.source, "C2", "C3", output=output)
    assert report["execution_status"] == "stopped" and report["completed_steps"] == 1
    path = output / report["finalization"]["receipt"]
    payload = json.loads(path.read_bytes())["payload"]
    payload["execution_status"] = "completed"
    path.write_text(json.dumps(envelope(payload)))
    with RecordStore.open(output / "record") as store:
        checked = store.check_integrity()
        assert not checked.ok
        assert "suppresses persisted execution evidence" in str(checked.issues)


def test_failed_publication_preserves_an_existing_receipt(tmp_path):
    path = tmp_path / "receipt.json"
    document = {"already": "published"}
    io.write_json(path, document)
    before = path.read_bytes()
    result = io.publish_receipt_json(path, document)
    assert result["status"] == "unconfirmed"
    assert path.read_bytes() == before


@pytest.mark.parametrize("known_fault", [False, True])
def test_session_receipt_failure_leaves_explicit_unconfirmed_evidence(
    tmp_path, replay_session, monkeypatch, known_fault,
):
    if known_fault:
        replay_session.fault = "tool"
    def unavailable(*args):
        return {"status": "unconfirmed", "published": False, "directory_synced": False,
                "errors": ["receipt disk fault"]}
    monkeypatch.setattr(session, "publish", unavailable)
    output = tmp_path / "output"
    report = session.run_replay(replay_session.source, "C0", "C3", output=output)
    assert report["execution_status"] == "failed"
    assert report["finalization"]["confirmation"] == "unconfirmed"
    view = replay_view(output)
    assert view["execution_status"] == ("failed" if known_fault else "unconfirmed")
    assert view["finalization"]["status"] == "unconfirmed"
