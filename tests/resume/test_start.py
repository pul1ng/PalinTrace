import pytest

from palinreplay.core.errors import RecordUsageError
from palinreplay.resume.start import _select_source
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake


def history(store, count=20):
    agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
    rec = fake.recorder(store, agent, backend)
    rec.start()
    for _ in range(count):
        fake.answer(rec, agent, ())
        rec.commit_step()
    return rec, agent


@pytest.fixture
def record(tmp_path):
    path = tmp_path / "run" / "record"
    with RecordStore.create(path) as store:
        rec, _ = history(store)
        node = rec.current_node
        rec.finish()
    return path, node


def test_c20_and_full_id_from_recording_or_store_directory(record):
    path, node = record
    for directory in (path.parent, path, str(path)):
        source, selected = _select_source(directory, "C20")
        assert source == path
        assert selected == node
        assert _select_source(directory, node.node_id) == (source, selected)


def test_relative_path_keeps_absolute_source(record, monkeypatch):
    path, node = record
    monkeypatch.chdir(path.parent)
    assert _select_source("record", "C20") == (path, node)


def test_c20_requires_run_only_when_selector_is_ambiguous(record):
    path, first = record
    with RecordStore.open(path, read_only=False) as store:
        rec, _ = history(store)
        second = rec.current_node
        rec.finish()
    with pytest.raises(RecordUsageError, match=r"matches 2 committed nodes.*use --run"):
        _select_source(path, "C20")
    for node in (first, second):
        assert _select_source(path, "C20", run_id=node.run_id) == (path, node)
        assert _select_source(path, node.node_id) == (path, node)
    with pytest.raises(RecordUsageError, match="no committed node"):
        _select_source(path, first.node_id, run_id=second.run_id)


@pytest.mark.parametrize("selector,run_id", [("C21", None), ("unknown", None), ("C20", "unknown")])
def test_missing_node_is_rejected(record, selector, run_id):
    with pytest.raises(RecordUsageError, match="no committed node"):
        _select_source(record[0], selector, run_id=run_id)


def test_uncommitted_step_is_not_selectable(tmp_path):
    path = tmp_path / "record"
    with RecordStore.create(path) as store:
        rec, agent = history(store, count=1)
        step = fake.answer(rec, agent, ("not-executed",))
        assert _select_source(path, "C1")[1] == rec.current_node
        for selector in ("C2", step.step.step_id):
            with pytest.raises(RecordUsageError, match="no committed node"):
                _select_source(path, selector)
        rec.end_incomplete("uncommitted fixture tail")
