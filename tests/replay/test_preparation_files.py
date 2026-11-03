from palinreplay.core.models import Node, State
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake
from tests.record.test_source_reference import configuration, recorder


def test_preparation_changes_enter_first_actual_snapshot_as_net_state(tmp_path):
    config = configuration(tmp_path, end=21)
    config["replay"]["preparation"] = {
        "config": {"services": [{"name": "A"}]},
        "evidence": {"status": "ready", "output_base64": "cmVhZHk="},
    }
    agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
    with RecordStore.create(tmp_path / "actual") as store:
        rec = recorder(store, config, agent, backend)
        backend.files["/service.pid"] = {"hex": b"actual identity".hex(), "mode": 0o600}
        backend.files["/temporary"] = {"hex": "41", "mode": 0o644}
        rec.start_from_source()
        assert not backend.captures and not store.list(Node)
        fake.answer(rec, agent, ())
        del backend.files["/temporary"]
        node = rec.commit_step()
        files = store.read_json(store.get(State, node.state_id).artifacts[0])
        assert node.index == 21 and len(backend.captures) == 1
        assert files["/service.pid"]["hex"] == b"actual identity".hex()
        assert "/temporary" not in files
        assert len(store.list(Node)) == 1
        assert store.read_json(rec.run.config)["replay"]["preparation_state_captured"] is False
        rec.finish()
        rec.close()
