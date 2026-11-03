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
@pytest.mark.parametrize("phase", ["validation", "envelope", "encoding", "link"])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, OSError])
def test_publication_exception_cannot_replace_the_final_outcome(
    tmp_path, replay_session, monkeypatch, known_fault, phase, error_type,
):
    from palinreplay.cli import replay as cli
    from palinreplay.storage import replay_finalization as finalization

    if known_fault:
        replay_session.fault = "tool"
    events = []
    class Progress:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def __call__(self, event):
            events.append(event)
    monkeypatch.setattr(cli, "ReplayProgressDisplay", lambda *a: Progress())
    def interrupted(*args, **kwargs):
        raise error_type("receipt publication interrupted")
    output = tmp_path / "output"
    with monkeypatch.context() as inject:
        if phase == "validation":
            inject.setattr(finalization.Receipt, "model_validate", interrupted)
        elif phase == "envelope":
            inject.setattr(finalization, "envelope", interrupted)
        elif phase == "link":
            link = io.os.link
            def broken_link(source, destination, **kwargs):
                if destination.name.startswith("replay-finalization-"):
                    interrupted()
                return link(source, destination, **kwargs)
            inject.setattr(io.os, "link", broken_link)
        else:
            encode = io.json_bytes
            def broken_encode(value):
                if isinstance(value, dict) and set(value) == {"payload", "sha256"}:
                    interrupted()
                return encode(value)
            inject.setattr(io, "json_bytes", broken_encode)
        code = main(["replay", str(replay_session.source), "--from", "C0", "--to", "C3",
                     "--output", str(output)])
    report = json.loads((output / "report.json").read_bytes())
    assert code == 1 and report["execution_status"] == "failed"
    assert bool(report.get("cancellation")) is (error_type is KeyboardInterrupt)
    assert report["outcome_evidence"]["faults"]
    assert [e.status for e in events if e.phase == "finished"] == ["failed"]
    replay = replay_view(output)
    assert replay["execution_status"] == ("failed" if known_fault else "unconfirmed")
    assert replay["finalization"]["status"] == "unconfirmed"
    with RecordStore.open(output / "record") as store:
        assert len(store.list(RunEnd)) == 1
    assert len(replay_session.sandbox.calls) == (2 if known_fault else 4)


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


def test_large_failure_receipt_is_readable_and_exportable(
    tmp_path, replay_session, capsys, record_property,
):
    from hashlib import sha256

    from palinreplay.core.models import Step
    from palinreplay.core.serialization import json_bytes

    calls = 0
    def failed_tool(request):
        nonlocal calls
        calls += 1
        raise RuntimeError("E" * 900_000)
    replay_session.executor.execute = failed_tool
    output, exported = tmp_path / "output", tmp_path / "export"
    replay_code = main(["replay", str(replay_session.source), "--from", "C0", "--to", "C1",
                        "--output", str(output), "--progress", "off"])
    capsys.readouterr()  # Discard the long CLI diagnostic instead of logging it on failure.
    report = json.loads((output / "report.json").read_bytes())
    path = output / report["finalization"]["receipt"]
    raw = path.read_bytes()
    receipt = json.loads(raw)
    assert receipt["sha256"] == sha256(json_bytes(receipt["payload"])).hexdigest()
    assert len(raw) > 2 * 1024**2
    assert receipt["payload"]["execution_status"] == report["execution_status"] == "failed"
    with RecordStore.open(output / "record") as store:
        steps, nodes, ends = store.list(Step), store.list(Node), store.list(RunEnd)
        assert (len(steps), len(nodes), len(ends), ends[0].status) == (1, 0, 1, "failed")
        details = [store.read_json(f.details) for f in store.list(Failure)]
        diagnostic = next(d["error"]["message"] for d in details
                          if isinstance(d.get("error"), dict)
                          and d["error"].get("type") == "builtins.RuntimeError")
        assert len(diagnostic) == 900_000
        assert sha256(diagnostic.encode()).hexdigest() == sha256(b"E" * 900_000).hexdigest()
    inspect_code = main(["record", "inspect", str(output / "record"), "--check", "--json"])
    inspected = capsys.readouterr()
    export_code = main(["record", "export", str(output / "record"), "--output", str(exported)])
    capsys.readouterr()
    metrics = {"replay_exit": replay_code, "inspect_exit": inspect_code, "export_exit": export_code,
               "tool_calls": calls, "receipt_bytes": len(raw)}
    for key, value in metrics.items():
        record_property(key, value)
    assert (replay_code, inspect_code, export_code, calls) == (1, 0, 0, 1), metrics
    checked = json.loads(inspected.out)
    assert checked["verification"]["ok"]
    replay = checked["runs"][0]["replay"]
    trajectory = json.loads((exported / "trajectory.json").read_bytes())
    assert trajectory["runs"][0]["replay"] == replay
    assert replay["execution_status"] == "failed" and replay["finalization"]["status"] == "confirmed"
    assert report["incomplete_step"] and report["statistics"]["actual_tool_executions"] == 1


@pytest.mark.parametrize("limit_delta", [0, -1], ids=["exact-limit", "one-byte-over"])
def test_receipt_encoded_byte_limit_preserves_the_recorded_failure(
    tmp_path, replay_session, monkeypatch, capsys, limit_delta,
):
    from palinreplay.core.serialization import json_bytes

    message = "故障" * 100
    calls, sizes = 0, []
    def failed_tool(request):
        nonlocal calls
        calls += 1
        raise RuntimeError(message)
    replay_session.executor.execute = failed_tool
    publish = session.publish
    def limited_publication(root, payload):
        size = len(json_bytes(envelope(payload)))
        sizes.append(size)
        monkeypatch.setattr(io, "MAX_RECEIPT_BYTES", size + limit_delta)
        return publish(root, payload)
    monkeypatch.setattr(session, "publish", limited_publication)
    output = tmp_path / "output"
    code = main(["replay", str(replay_session.source), "--from", "C0", "--to", "C1",
                 "--output", str(output), "--progress", "off"])
    capsys.readouterr()
    report = json.loads((output / "report.json").read_bytes())
    assert code == 1 and calls == 1 and len(sizes) == 1
    assert report["execution_status"] == "failed"
    path = output / report["finalization"]["receipt"]
    if limit_delta == 0:
        assert path.stat().st_size == sizes[0] == io.MAX_RECEIPT_BYTES
    else:
        assert not path.exists() and not list(path.parent.glob(".receipt-*"))
        assert report["finalization"]["confirmation"] == "unconfirmed"
        assert str(sizes[0]) in str(report["receipt_publication"]["errors"])
        assert f"{io.MAX_RECEIPT_BYTES}-byte limit" in str(report["receipt_publication"]["errors"])
    inspected = replay_view(output)
    assert inspected["execution_status"] == "failed"
    assert inspected["finalization"]["status"] == ("confirmed" if limit_delta == 0 else "unconfirmed")
    with RecordStore.open(output / "record") as store:
        assert len(store.list(RunEnd)) == 1 and store.list(RunEnd)[0].status == "failed"
        details = [store.read_json(f.details) for f in store.list(Failure)]
        assert any(d.get("error", {}).get("message") == message for d in details)
    assert main(["record", "export", str(output / "record"),
                 "--output", str(tmp_path / "export")]) == 0
    capsys.readouterr()


def test_receipt_reader_enforces_the_publication_byte_limit(tmp_path, monkeypatch):
    from palinreplay.core.errors import IntegrityError
    from palinreplay.core.serialization import json_bytes

    path = tmp_path / "receipt.json"
    document = {"payload": {"error": "故障" * 10}, "sha256": "a" * 64}
    raw = json_bytes(document)
    monkeypatch.setattr(io, "MAX_RECEIPT_BYTES", len(raw) - 1)
    with pytest.raises(ValueError, match="byte limit"):
        io.publish_receipt_json(path, document)
    assert not path.exists() and not list(tmp_path.glob(".receipt-*"))
    # Simulate an existing file: readers enforce precisely the same byte cap.
    io.write_json(path, document)
    with pytest.raises(IntegrityError):
        io.read_receipt_bytes(path)
    assert path.read_bytes() == raw


def test_large_receipt_reconciliation_uses_the_same_read_limit(tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"
    document = {"error": "E" * (2 * 1024**2)}
    sync = io.sync_directory
    synchronized = 0
    def failed_first_sync(directory):
        nonlocal synchronized
        synchronized += 1
        if synchronized == 1:
            assert path.stat().st_size > 2 * 1024**2
            raise OSError("directory sync fault after large receipt publication")
        return sync(directory)
    monkeypatch.setattr(io, "sync_directory", failed_first_sync)
    result = io.publish_receipt_json(path, document)
    assert result["status"] == "unconfirmed" and not result["published"]
    assert not path.exists() and synchronized == 2
    assert len(result["errors"]) == 1  # Reconciliation read succeeds, without a retraction error.


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_receipt_reader_requires_a_regular_file(tmp_path, kind):
    from palinreplay.core.errors import IntegrityError

    path = tmp_path / "receipt.json"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text("{}")
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    else:
        io.os.mkfifo(path)
    with pytest.raises(IntegrityError):
        io.read_receipt_bytes(path)


@pytest.mark.parametrize("change", [0, 1, -1], ids=["stable", "growing", "shrinking"])
def test_receipt_reader_uses_actual_size_and_rejects_size_changes(tmp_path, monkeypatch, change):
    from palinreplay.core.errors import IntegrityError

    path = tmp_path / "receipt.json"
    path.write_bytes(b"{}")
    opened, requests = io.os.fdopen, []
    def observe_read(fd, mode):
        source = opened(fd, mode)
        class ObservedFile:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                source.close()
            def read(self, size):
                requests.append(size)
                if change:
                    path.write_bytes(b"{} " if change > 0 else b"{")
                return source.read(size)
        return ObservedFile()
    monkeypatch.setattr(io.os, "fdopen", observe_read)
    if change:
        with pytest.raises(IntegrityError, match="size changed"):
            io.read_receipt_bytes(path)
    else:
        assert io.read_receipt_bytes(path) == b"{}"
    assert requests == [3], "the read buffer must follow actual file size, not the 512 MiB cap"
