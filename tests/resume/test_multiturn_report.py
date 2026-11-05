import json
from copy import deepcopy

import pytest

from palinreplay.core.models import Node, Operation, RunEnd
from palinreplay.resume import run_resume_steps
from palinreplay.sandbox.base import ExecResult
from palinreplay.storage.store import RecordStore
from tests.resume import test_run_steps

session = test_run_steps.session
llm_config = test_run_steps.llm_config
execution_config = test_run_steps.execution_config
recordable = test_run_steps.recordable


@pytest.fixture
def response(monkeypatch):
    monkeypatch.setattr("palinreplay.resume.decision.decision_cost",
                        lambda model, reply: {"cost": .25 * int(reply.id.split("-")[-1]),
                                              "cost_known": True})
    return test_run_steps.model_response(1, ["first"])


@pytest.mark.parametrize("recordable", [{"tools": 1, "max_steps": 3}], indirect=True)
@pytest.mark.parametrize("unknown_last", [False, True])
def test_totals_match_variable_tool_groups_and_keep_inherited_usage_separate(
    recordable, llm_config, monkeypatch, unknown_last,
):
    case = recordable
    first_evidence = deepcopy(case.session._decision_evidence)
    if unknown_last:
        monkeypatch.setattr("palinreplay.resume.decision.decision_cost",
                            lambda model, reply: {"cost": None if reply.id == "round-3" else .5,
                                                  "cost_known": reply.id != "round-3"})
    case.complete.side_effect = [test_run_steps.model_response(2, ["second-a", "second-b"]),
                                 test_run_steps.model_response(3, ["third-a", "third-b", "third-c"])]
    case.session.executor.execute.side_effect = lambda request: ExecResult("tool", b"actual output\n", 0, .1)
    nodes = run_resume_steps(case.session, case.decision, llm_config=llm_config)
    report = case.session.report["recording"]
    assert report["total_model_requests"] == case.complete.call_count == 3
    assert report["model_requests"] == report["recording_model_requests"] == 2
    assert report["decisions"] == 3 and report["remaining_steps"] == 0
    assert report["tool_executions"] == report["tool_results"] == report["accepted_tools"] == 6
    assert report["recorded_tool_dispatches"] == case.session.executor.execute.call_count == 6
    assert report["complete_nodes"] == 3
    assert report["last_complete_node"] == nodes[-1].model_dump(mode="json")
    assert report["stop_reason"] == "budget_exhausted" and report["run_status"] == "paused"
    assert report["inherited_counters"] == {"n_calls": 20, "cost": 0, "consecutive_format_errors": 0}
    assert report["known_cost"] == (.75 if unknown_last else 1.5)
    assert report["cost_known"] is not unknown_last
    assert report["cost"] == (None if unknown_last else 1.5)
    assert [item["usage"]["total_tokens"] for item in report["request_accounting"]] == [15, 15, 15]
    assert len(report["request_accounting"]) == 3
    with RecordStore.open(case.session.output / "record") as store:
        assert len(store.list(Node)) == report["complete_nodes"]
        operations = store.list(Operation)
        assert sum(op.kind == "model" for op in operations) == 3
        assert sum(op.kind == "tool" for op in operations) == 6
        assert len(store.list(RunEnd)) == 1
        budgets = [store.read_json(node.context)["resume"] for node in nodes]
        assert [resume["remaining_steps"] for resume in budgets] == [2, 1, 0]
        assert budgets[-1]["remaining_steps"] == report["remaining_steps"]
        assert all(resume["first_decision_accounting"] == case.decision.accounting for resume in budgets)
        context = store.read_json(nodes[-1].context)
        assert len(context["messages"]) == report["message_count"] == 51
        assert context["counters"]["n_calls"] == 23
    assert case.session._decision_evidence == first_evidence
    persisted = json.loads(case.session.report_path.read_text())["recording"]
    assert persisted == report


@pytest.mark.parametrize("recordable", [{"tools": 1, "max_steps": 3}], indirect=True)
def test_failed_request_is_counted_with_unknown_usage_and_last_complete_node(recordable, llm_config):
    case = recordable
    case.complete.side_effect = RuntimeError("model unavailable")
    case.session.executor.execute.side_effect = [ExecResult("first", b"first output\n", 0, .1)]
    with pytest.raises(RuntimeError, match="model unavailable"):
        run_resume_steps(case.session, case.decision, llm_config=llm_config)
    report = case.session.report["recording"]
    assert report["total_model_requests"] == 2 and report["decisions"] == 1
    assert report["remaining_steps"] == 1
    assert report["known_cost"] == .25 and report["cost"] is None and not report["cost_known"]
    assert report["request_accounting"][-1]["usage"] is None
    assert report["stop_reason"] == "failed" and report["failure_stage"] == "model"
    assert report["complete_nodes"] == 1 and report["last_complete_node"]["index"] == 1
