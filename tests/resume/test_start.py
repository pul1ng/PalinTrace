import sqlite3
from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import pytest

from palinreplay.core.errors import IntegrityError, RecordUsageError
from palinreplay.core.interfaces import BoundaryAdapter, Payload
from palinreplay.core.models import Node, Run, State
from palinreplay.llm import OpenAIChatClient
from palinreplay.record.recorder import Recorder
from palinreplay.resume.start import _ResumeStart, _select_source
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


@pytest.fixture
def metadata_only(record, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Resume selection read or verified artifact materials")

    for method in ("read_bytes", "read_json", "verify_artifact", "get_node"):
        monkeypatch.setattr(RecordStore, method, unexpected)
    monkeypatch.setattr("palinreplay.storage.btrfs.verification.verify_state", unexpected)


def test_source_references_share_one_read_only_snapshot(record, metadata_only, monkeypatch):
    path, expected = record
    with RecordStore.open(path) as store:
        run = store.get(Run, expected.run_id)
        state = store.get(State, expected.state_id)
    original_get = RecordStore.get
    reads = []

    def track_get(source, model, record_id):
        assert source.read_only
        assert source._db.in_transaction
        value = original_get(source, model, record_id)
        reads.append((source, model, value))
        return value

    monkeypatch.setattr(RecordStore, "get", track_get)
    source_path, node = _select_source(path, "C20")
    assert source_path == path
    assert node == expected
    assert node.context == expected.context
    assert node.input_position == expected.input_position
    values = {model: value for _, model, value in reads}
    assert values == {Node: node, Run: run, State: state}
    assert node is values[Node]
    source = reads[0][0]
    assert all(reader is source for reader, _, _ in reads)
    assert source._closed


@pytest.mark.parametrize("model", [Run, State])
def test_missing_source_record_fails_and_closes_reader(record, metadata_only, monkeypatch, model):
    path, node = record
    record_id = node.run_id if model is Run else node.state_id
    with sqlite3.connect(path / "catalog.sqlite") as catalog:
        catalog.execute("DELETE FROM records WHERE kind=? AND id=?", (
            model.__name__.lower(), record_id,
        ))
    original_open = RecordStore.open
    readers = []

    def track_open(cls, path, *, read_only=True):
        reader = original_open(path, read_only=read_only)
        readers.append(reader)
        return reader

    monkeypatch.setattr(RecordStore, "open", classmethod(track_open))
    with pytest.raises(IntegrityError, match=f"missing record: {model.__name__}"):
        _select_source(path, "C20")
    assert len(readers) == 1
    assert readers[0].read_only and readers[0]._closed


def test_start_keeps_callers_model_without_requests_or_api_key(tmp_path, monkeypatch):
    path = tmp_path / "record"
    agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
    with RecordStore.create(path) as store:
        with Recorder(
            store, task_id="resume-model", agent_name="fixture-agent", agent_version="1",
            config=Payload.json({"model": "A"}),
            boundary=BoundaryAdapter(agent.export, backend.capture),
        ) as rec:
            fake.answer(rec, agent, ())
            rec.commit_step()
            rec.finish()
        assert store.read_json(rec.run.config) == {"model": "A"}
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    model_request = Mock(side_effect=AssertionError("Resume selection requested a model"))
    monkeypatch.setattr(OpenAIChatClient, "complete", model_request)

    source, node = _select_source(path, "C1")
    start = _ResumeStart(source_store=source, node=node, model="B", max_steps=2)
    assert start.source_store == path
    assert start.node is node
    assert start.node.context is node.context
    assert start.node.input_position is node.input_position
    assert start.model == "B"
    model_request.assert_not_called()
    with pytest.raises(FrozenInstanceError):
        start.model = "A"


@pytest.mark.parametrize("max_steps", [0, -1, True, False, 2.0, "2", None])
def test_new_budget_requires_a_positive_integer(record, max_steps):
    path, node = record
    with pytest.raises(RecordUsageError, match="max_steps must be a positive integer"):
        _ResumeStart(source_store=path, node=node, model="M", max_steps=max_steps)


@pytest.mark.parametrize("finished", [False, True])
def test_c1_accepts_two_new_decisions_without_a_historical_c2(tmp_path, finished):
    path = tmp_path / "record"
    with RecordStore.create(path) as store:
        rec, _ = history(store, count=1)
        if finished:
            rec.finish()
        source, node = _select_source(path, "C1")
        start = _ResumeStart(source_store=source, node=node, model="M", max_steps=2)
        assert start.node == rec.current_node
        assert start.node.index == 1
        assert start.max_steps == 2
        with pytest.raises(RecordUsageError, match="no committed node"):
            _select_source(path, "C2")
        if not finished:
            rec.finish()
