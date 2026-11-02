"""Offline counterexamples for the real service acceptance's pass criteria."""

import importlib
import json
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture
def fixture_module(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "examples"))
    return importlib.import_module("replay_service_fixture")


@pytest.fixture
def evidence(fixture_module):
    source = {"exit_code": 0, "output": json.dumps({"status": 200, "body": fixture_module.BODY}),
              "definitive": True}
    actual = {"exit_code": 1, "output": json.dumps({"error": "ConnectionRefusedError", "errno": 111}),
              "definitive": True}
    report = {
        "status": "difference", "stop_reason": "tool-result", "incomplete_step": False,
        "steps": [{"source_index": 3, "tools": [{"definitive": True}]}],
        "mappings": [{"source_index": 2, "index": 0}, {"source_index": 3, "index": 1}],
        "statistics": {"actual_tool_executions": 1, "historical_answer_supplies": 1,
                       "actual_model_calls": 0, "actual_model_cost": 0},
        "cleanup_confirmed": True, "cleanup_errors": [],
    }
    r1 = {"status": "passed", "files": {"equal": True}, "root_attributes": {"equal": True},
          "context_checks": {"messages": True, "counters": True}}
    return {"source_tool": source, "actual_tool": actual, "report": report, "r1": r1}


def test_only_expected_missing_service_result_passes(fixture_module, evidence):
    result = fixture_module.classify_result(**evidence)
    assert result["status"] == "passed"
    assert all(result["checks"].values())


@pytest.mark.parametrize("error,number", [
    ("TimeoutError", None), ("FileNotFoundError", 2), ("ConnectionResetError", 104),
    ("ConnectionRefusedError", 110), ("OSError", 111),
])
def test_other_failures_are_not_missing_service(fixture_module, evidence, error, number):
    evidence["actual_tool"]["output"] = json.dumps({"error": error, "errno": number})
    result = fixture_module.classify_result(**evidence)
    assert result["status"] == "failed"
    assert not result["checks"]["connection_refused"]


@pytest.mark.parametrize("output", ["", "connection refused", "[]", "null", "{broken"])
def test_non_structured_returns_do_not_pass(fixture_module, evidence, output):
    evidence["actual_tool"]["output"] = output
    assert fixture_module.classify_result(**evidence)["status"] == "failed"


@pytest.mark.parametrize("code,definitive", [(0, True), (None, False), (1, False)])
def test_success_or_unknown_result_does_not_pass(fixture_module, evidence, code, definitive):
    evidence["actual_tool"].update(exit_code=code, definitive=definitive)
    assert fixture_module.classify_result(**evidence)["status"] == "failed"


def test_unexpected_success_does_not_pass(fixture_module, evidence):
    evidence["actual_tool"] = deepcopy(evidence["source_tool"])
    assert fixture_module.classify_result(**evidence)["status"] == "failed"


@pytest.mark.parametrize("field,value", [
    ("status", "failed"), ("status", "match"), ("stop_reason", "file-state"),
    ("incomplete_step", True), ("cleanup_confirmed", False),
    ("cleanup_errors", ["pool cleanup failed"]),
])
def test_replay_failures_and_incomplete_groups_do_not_pass(fixture_module, evidence, field, value):
    evidence["report"][field] = value
    assert fixture_module.classify_result(**evidence)["status"] == "failed"


@pytest.mark.parametrize("field", ["status", "files", "root_attributes", "context_checks"])
def test_r1_failures_cannot_be_reported_as_expected_divergence(fixture_module, evidence, field):
    evidence["r1"][field] = {
        "status": "failed", "files": {"equal": False}, "root_attributes": {"equal": False},
        "context_checks": {"messages": False},
    }[field]
    assert fixture_module.classify_result(**evidence)["status"] == "failed"


@pytest.mark.parametrize("status,body", [(500, "PALIN_SERVICE_OK"), (200, "wrong body")])
def test_source_http_failure_does_not_pass(fixture_module, evidence, status, body):
    evidence["source_tool"]["output"] = json.dumps({"status": status, "body": body})
    assert fixture_module.classify_result(**evidence)["status"] == "failed"


def test_source_nonzero_or_uncertain_return_does_not_pass(fixture_module, evidence):
    evidence["source_tool"]["exit_code"] = 1
    assert fixture_module.classify_result(**evidence)["status"] == "failed"
    evidence["source_tool"].update(exit_code=0, definitive=False)
    assert fixture_module.classify_result(**evidence)["status"] == "failed"


def test_C4_execution_is_rejected(fixture_module, evidence):
    evidence["report"]["mappings"].append({"source_index": 4, "index": 2})
    evidence["report"]["steps"].append({"source_index": 4, "tools": [{"definitive": True}]})
    evidence["report"]["statistics"].update(actual_tool_executions=2, historical_answer_supplies=2)
    assert fixture_module.classify_result(**evidence)["status"] == "failed"


@pytest.mark.parametrize("field,value", [
    ("actual_tool_executions", 2), ("historical_answer_supplies", 2),
    ("actual_model_calls", 1), ("actual_model_cost", 0.1),
])
def test_extra_execution_or_model_usage_is_rejected(fixture_module, evidence, field, value):
    evidence["report"]["statistics"][field] = value
    assert fixture_module.classify_result(**evidence)["status"] == "failed"


def test_unconfirmed_source_cleanup_stops_before_replay(fixture_module, tmp_path, monkeypatch):
    (tmp_path / "report.json").write_text(json.dumps({"container_id": "source", "cleanup_confirmed": True}))
    monkeypatch.setattr(fixture_module, "absent", lambda *args: False)
    with pytest.raises(AssertionError, match="Replay is forbidden"):
        fixture_module.source_identity(tmp_path, object())
    assert not (tmp_path / "identity.json").exists()


def test_native_model_transport_is_forbidden(fixture_module):
    from palinreplay.agents.mini import load_native

    with pytest.raises(AssertionError, match="external model calls are forbidden"):
        with fixture_module.no_model_calls():
            load_native().LitellmModel._query(None, [])
