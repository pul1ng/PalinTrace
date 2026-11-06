from types import SimpleNamespace

import pytest

from examples import resume_restore_validation as validation
from palinreplay.core.errors import IntegrityError, RecordUsageError
from palinreplay.core.models import Run
from palinreplay.llm import OpenAIChatClient
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.storage.store import RecordStore
from tests.record import test_restore_materials as fixtures

record = fixtures.record


@pytest.mark.parametrize("location", ["store", "recording", "symlink"])
def test_acceptance_rejects_source_outputs_before_writing(tmp_path, location):
    source = tmp_path / "run" / "record"
    with RecordStore.create(source):
        pass
    output = source / "out" if location == "store" else source.parent / "out"
    if location == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(source.parent, target_is_directory=True)
        output = alias / "out"
    before = validation.file_inventory(source.parent)
    with pytest.raises(RecordUsageError):
        validation.main(source, output)
    assert not output.exists() and validation.file_inventory(source.parent) == before


def test_guards_count_and_reject_unapproved_work():
    counts = dict.fromkeys(("model_requests", "historical_tool_reexecutions", "service_preparations",
                           "branch_node_commits", "native_initializations"), 0)
    with validation.no_new_work(counts):
        from palinreplay.agents.mini import load_native
        from palinreplay.restoration.services import prepare_services

        for call in (lambda: OpenAIChatClient.complete(None),
                     lambda: RestoredExecutor.exec(object(), "history command"),
                     lambda: prepare_services(None, None, None),
                     lambda: RecordStore.commit_node(None, None), load_native):
            with pytest.raises(AssertionError, match="attempted"):
                call()
    assert set(counts.values()) == {1}


@pytest.mark.parametrize("fault", ["missing", "digest", "json"])
def test_material_damage_only_changes_copies(record, tmp_path, monkeypatch, fault):
    path, (run, node, *_), _ = record
    before = validation.file_inventory(path)
    monkeypatch.setattr(validation, "select_resume_start", lambda copy, *a, **k: copy)
    copy = validation.corrupt_copy(SimpleNamespace(source_store=path, node=node), tmp_path, fault)
    assert validation.file_inventory(path) == before
    with RecordStore.open(copy, read_only=True) as store:
        ref = store.get(Run, run.run_id).config if fault == "json" else node.context
        with pytest.raises(IntegrityError):
            store.read_json(ref)
