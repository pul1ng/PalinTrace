import hashlib
import json
from pathlib import Path

import pytest

from palinreplay.core.errors import RecordUsageError
from palinreplay.core.models import Node, Run, State
from palinreplay.preprocess.source import pin_source
from palinreplay.replay.plan import pin_replay
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake


def digest_tree(root: Path) -> dict:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_pins_full_committed_range_and_binds_its_provenance(tmp_path):
    fake.main(tmp_path / "smoke")
    root = tmp_path / "smoke/record"
    before = digest_tree(root)
    with RecordStore.open(root) as store:
        source = pin_source(store)
        expected = pin_replay(store, "C0").provenance()
        assert source.run.run_id == store.list(Run)[0].run_id
        legacy_selection = {
            "schema_version": "palinreplay.replay-source.v1",
            "source_store": str(root.resolve()),
            "run": source.run.model_dump(mode="json"),
            "nodes": [node.model_dump(mode="json") for node in source.nodes],
            "states": [store.get(State, node.state_id).model_dump(mode="json") for node in source.nodes],
        }
        canonical = (json.dumps(legacy_selection, sort_keys=True, ensure_ascii=True,
                                separators=(",", ":")) + "\n").encode()
        assert expected["digest_scheme"] == "sha256-canonical-selection-json-v1"
        assert expected["selection_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert source.start_index == 0 and source.end_index == 3
    assert source.document() == {
        "run_id": expected["run"]["run_id"],
        "start_index": 0,
        "end_index": 3,
        "digest_scheme": expected["digest_scheme"],
        "selection_sha256": expected["selection_sha256"],
    }
    assert digest_tree(root) == before


def test_ignores_uncommitted_tail(tmp_path):
    root = tmp_path / "record"
    with RecordStore.create(root) as store:
        rec = fake.recorder(store, fake.FakeAgent(), fake.FakeStateBackend())
        rec.start()
        rec.begin_step()
        rec.end_incomplete("uncommitted fixture tail")
        with RecordStore.open(root) as reader:
            assert [node.index for node in reader.list(Node)] == [0]
            source = pin_source(reader)
            assert source.end_index == 0
            assert source.document()["end_index"] == 0


def test_multiple_runs_require_an_explicit_selection(tmp_path):
    root = tmp_path / "record"
    with RecordStore.create(root) as store:
        selected = []
        for _ in range(2):
            rec = fake.recorder(store, fake.FakeAgent(), fake.FakeStateBackend())
            rec.start()
            rec.finish()
            selected.append(rec.run.run_id)
        with RecordStore.open(root) as reader:
            with pytest.raises(RecordUsageError, match="--run"):
                pin_source(reader)
            source = pin_source(reader, selected[1])
            assert source.run.run_id == selected[1]
            assert source.start_index == source.end_index == 0


def test_unknown_run_and_wrong_digest_scope_are_rejected(tmp_path):
    fake.main(tmp_path / "smoke")
    with RecordStore.open(tmp_path / "smoke/record") as store:
        with pytest.raises(RecordUsageError, match="not present"):
            pin_source(store, "missing-run")
        full = pin_source(store).document()
        partial = pin_replay(store, "C1", "C3").provenance()
    assert full["start_index"] == 0 and partial["nodes"][0]["index"] == 1
    assert full["selection_sha256"] != partial["selection_sha256"]
