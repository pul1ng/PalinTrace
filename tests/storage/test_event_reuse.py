import pytest

from palinreplay.core.models import Event
from palinreplay.storage.store import IntegrityError, RecordStore
from tests.storage.test_store import artifact_path, begin_step, call, start


def test_append_reuses_one_validated_event_list_and_preserves_sequence(tmp_path, monkeypatch):
    with RecordStore.create(tmp_path / "record") as store:
        run, attempt, _, node = start(store)
        step = begin_step(store, run, attempt, node)
        execution = call(store, step, stages=0)
        original = store.list
        reads = []

        def listed(model, **kwargs):
            if model is Event:
                reads.append(kwargs)
            return original(model, **kwargs)

        monkeypatch.setattr(store, "list", listed)
        for index, kind in enumerate(("dispatched", "result", "delivered", "accepted"), 1):
            reads.clear()
            event = store.append_event(
                execution.execution_id, kind,
                payload=store.put_bytes(b"result") if kind != "dispatched" else None,
                outcome="returned" if kind == "result" else None,
            )
            assert event.sequence == index and event.kind == kind
            assert reads == [{"run_id": run.run_id}]
        assert [e.kind for e in store.execution_events(execution.execution_id)] == [
            "dispatched", "result", "delivered", "accepted",
        ]


@pytest.mark.parametrize("damage", ["catalog", "payload"])
def test_event_reuse_still_rejects_corruption_before_delivery(tmp_path, damage):
    with RecordStore.create(tmp_path / "record") as store:
        run, attempt, _, node = start(store)
        execution = call(store, begin_step(store, run, attempt, node), stages=2)
        result = store.execution_events(execution.execution_id)[-1]
        if damage == "catalog":
            store._db.execute(
                "UPDATE records SET sha256=? WHERE kind='event' AND id=?",
                ("0" * 64, result.event_id),
            )
        else:
            path = artifact_path(store, result.payload)
            path.chmod(0o600)
            path.write_bytes(b"corrupt")
        with pytest.raises(IntegrityError):
            store.append_event(execution.execution_id, "delivered", payload=store.put_bytes(b"view"))
        assert store._db.execute("SELECT COUNT(*) FROM records WHERE kind='event'").fetchone()[0] == 2
