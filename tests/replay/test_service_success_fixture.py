"""The success acceptance must prove readiness without consuming the first GET."""

import importlib
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture
def success_fixture(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "examples"))
    return importlib.import_module("replay_service_success_validation")


@pytest.fixture
def snapshot_rows():
    old = {"pid": 12, "start": "123"}
    new = {"pid": 14, "start": "456"}
    return {
        "source_C1": {"identity": old, "requests": []},
        "source_C2": {"identity": old, "requests": ["GET /"]},
        "source_C3": {"identity": None, "requests": ["GET /"]},
        "actual_C2": {"identity": new, "requests": ["GET /"]},
        "actual_C3": {"identity": None, "requests": ["GET /"]},
    }


def test_expected_snapshot_sequence_passes(success_fixture, snapshot_rows):
    success_fixture.check_snapshot_rows(snapshot_rows, replay=True)


@pytest.mark.parametrize("label,field,value", [
    ("source_C1", "requests", ["GET /"]),
    ("source_C2", "requests", []),
    ("actual_C2", "requests", []),
    ("actual_C2", "requests", ["GET /", "GET /"]),
    ("actual_C3", "requests", ["GET /", "GET /"]),
    ("source_C3", "identity", {"pid": 12, "start": "123"}),
    ("actual_C3", "identity", {"pid": 14, "start": "456"}),
    ("actual_C2", "identity", {"pid": 12, "start": "123"}),
])
def test_hidden_requests_and_stale_service_are_rejected(
    success_fixture, snapshot_rows, label, field, value,
):
    rows = deepcopy(snapshot_rows)
    rows[label][field] = value
    with pytest.raises(AssertionError):
        success_fixture.check_snapshot_rows(rows, replay=True)
