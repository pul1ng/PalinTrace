"""Resume preflight checks must finish before accessing Docker."""

import pytest

from palinreplay.core.errors import IntegrityError
from palinreplay.core.interfaces import Payload
from palinreplay.resume import select_resume_start
from palinreplay.resume.materials import load_resume_materials
from palinreplay.storage.store import RecordStore
from tests.record import test_restore_materials as fixtures
from tests.record.test_restore_materials import _record_run, _rewrite_record

record = fixtures.record
no_container = fixtures.no_container


def test_pinned_read_only_materials(record, no_container, monkeypatch):
    path, (run, node, _, state, _), _ = record
    start = select_resume_start(path, node.node_id, model="M", max_steps=2)
    original = RecordStore.open
    opens = []

    def opened(path, **options):
        opens.append(options)
        return original(path, **options)

    monkeypatch.setattr(RecordStore, "open", opened)
    materials = load_resume_materials(start)
    assert opens == [{"read_only": True}]
    assert (materials.run, materials.node, materials.state) == (run, node, state)
    assert materials.context == {"messages": ["initial"]}
    assert materials.config_payload.media_type == run.config.media_type


def test_rejects_changed_selected_references(record, no_container):
    path, (_, node, next_node, _, _), _ = record
    start = select_resume_start(path, node.node_id, model="M", max_steps=2)
    _rewrite_record(path, node.model_copy(update={"context": next_node.context}))
    with pytest.raises(IntegrityError, match="references changed"):
        load_resume_materials(start)


@pytest.mark.parametrize("role", ["config", "context", "input_position", "manifest", "runtime"])
@pytest.mark.parametrize("fault", ["missing", "digest"])
def test_bad_materials_fail_without_resources(record, no_container, role, fault):
    path, (run, node, _, state, _), _ = record
    start = select_resume_start(path, node.node_id, model="M", max_steps=2)
    owner = run if role == "config" else node if role in ("context", "input_position") else state
    ref = getattr(owner, role)
    target = path / "artifacts" / ref.sha256[:2] / ref.sha256[2:]
    target.unlink()
    if fault == "digest":
        target.write_bytes(b"x" * ref.size)
    with pytest.raises(IntegrityError):
        load_resume_materials(start)


@pytest.mark.parametrize("role", ["config", "context", "input_position"])
@pytest.mark.parametrize("data", [b"invalid json", b"[]"])
def test_resume_decodes_all_json_before_resources(tmp_path, monkeypatch, role, data):
    monkeypatch.setattr("palinreplay.storage.btrfs.verification.verify_state", lambda *a: None)
    with RecordStore.create(tmp_path / "source") as store:
        _, node, *_ = _record_run(store, "fixture", payloads={
            role: Payload(data, "application/json"),
        })
    start = select_resume_start(tmp_path / "source", node.node_id, model="M", max_steps=2)
    with pytest.raises(IntegrityError, match="JSON"):
        load_resume_materials(start)
