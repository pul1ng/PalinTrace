import pytest

from palinreplay.core.models import Node
from palinreplay.replay.plan import pin_replay
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake


def history(store, count=3):
    agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
    rec = fake.recorder(store, agent, backend)
    rec.start()
    for _ in range(count):
        fake.answer(rec, agent, ())
        rec.commit_step()
    return rec, agent


def test_labels_ids_and_bounds(tmp_path):
    with RecordStore.create(tmp_path / "record") as store:
        rec, _ = history(store)
        selected = pin_replay(store, "C1", "C2")
        assert [n.index for n in selected.nodes] == [1, 2]
        assert pin_replay(store, selected.start.node_id, selected.end.node_id) == selected
        assert pin_replay(store, "C2", "C2").nodes == (selected.end,)
        assert selected.provenance()["selection_sha256"] == selected.provenance()["selection_sha256"]
        for start, end in (("C3", "C2"), ("C999", None), ("C1", "C999")):
            with pytest.raises(ValueError):
                pin_replay(store, start, end)
        rec.finish()


def test_runs_and_uncommitted_tail(tmp_path):
    with RecordStore.create(tmp_path / "record") as store:
        first, agent = history(store)
        expected = pin_replay(store, "C1")
        fake.answer(first, agent, ("not-executed",))
        assert pin_replay(store, "C1") == expected
        first.end_incomplete("fixture tail")
        second, _ = history(store, 1)
        with pytest.raises(ValueError, match="exactly one"):
            pin_replay(store, "C0")
        assert pin_replay(store, "C1", run_id=first.run.run_id) == expected
        with pytest.raises(ValueError, match="no committed node"):
            pin_replay(store, expected.start.node_id, second.current_node.node_id)
        second.finish()


def test_one_read_view_excludes_concurrent_append(tmp_path, monkeypatch):
    root = tmp_path / "record"
    with RecordStore.create(root) as writer:
        writer._db.execute("PRAGMA journal_mode=WAL")
        rec, agent = history(writer, 1)
        with RecordStore.open(root) as reader:
            original = reader.list
            appended = False

            def append_then_list(model, **kwargs):
                nonlocal appended
                if model is Node and not appended:
                    appended = True
                    fake.answer(rec, agent, ())
                    rec.commit_step()
                return original(model, **kwargs)

            monkeypatch.setattr(reader, "list", append_then_list)
            selected = pin_replay(reader, "C0")
            digest = selected.provenance()["selection_sha256"]
            assert [n.index for n in selected.nodes] == [0, 1]
            assert pin_replay(reader, "C0").end.index == 2
            assert selected.end.index == 1
            assert selected.provenance()["selection_sha256"] == digest
        rec.finish()
