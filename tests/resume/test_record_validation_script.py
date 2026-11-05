from copy import deepcopy
from types import SimpleNamespace

import pytest

from examples import resume_record_validation as validation
from palinreplay.core.errors import RecordUsageError
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.resume import record_resume_step
from palinreplay.sandbox.base import ExecResult
from tests.resume import test_record_step

session = test_record_step.session
llm_config = test_record_step.llm_config
response = test_record_step.response
execution_config = test_record_step.execution_config
recordable = test_record_step.recordable


@pytest.fixture
def complete_cases():
    names = {source.name for source in validation.SOURCES} | set(validation.BOUNDARIES)
    cases = {name: {"status": "passed", "cleanup_confirmed": True, "restored": True,
                    "duplicate_rejected": True, "complete_nodes": 1,
                    "counts": {"model_requests": 1, "recording_model_requests": 0,
                               "tool_executions": 2, "complete_nodes": 1, "file_snapshots": 1}}
             for name in names}
    cases["c20"]["modified_rejected"] = True
    cases["closed"]["closed_rejected"] = True
    for name in ("interrupt", "adoption_failure", "snapshot_failure"):
        cases[name].update(complete_nodes=0, failed_decision_not_replayed=True)
    return cases


def test_final_gate_requires_the_actual_complete_matrix(complete_cases):
    assert validation.all_cases_passed(complete_cases)
    del complete_cases["astropy_long"]
    assert not validation.all_cases_passed(complete_cases)


@pytest.mark.parametrize("fault", ["tools", "model", "snapshot", "recovery", "cleanup", "partial", "duplicate", "tamper"])
def test_final_gate_rejects_incomplete_or_miscounted_evidence(complete_cases, fault):
    if fault == "tools":
        complete_cases["django_middle"]["counts"]["tool_executions"] = 3
    elif fault == "model":
        complete_cases["c20"]["counts"]["recording_model_requests"] = 1
    elif fault == "snapshot":
        complete_cases["astropy_initial"]["counts"]["file_snapshots"] = 0
    elif fault == "recovery":
        complete_cases["astropy_long"]["restored"] = False
    elif fault == "cleanup":
        complete_cases["closed"]["cleanup_confirmed"] = False
    elif fault == "partial":
        complete_cases["interrupt"]["complete_nodes"] = 1
    elif fault == "duplicate":
        complete_cases["c20"]["duplicate_rejected"] = False
    else:
        complete_cases["c20"]["modified_rejected"] = False
    assert not validation.all_cases_passed(complete_cases)


def test_recording_guard_counts_real_calls_and_interrupts_before_second_entry(monkeypatch):
    container = SimpleNamespace(id="actual-container")
    executor = SimpleNamespace(_container=container)
    session = SimpleNamespace(executor=executor, restored=SimpleNamespace(container=container))
    requests = []
    def execute(executor, request):
        requests.append(deepcopy(request))
        return ExecResult(request["action"]["command"], b"actual", 0, .1)
    monkeypatch.setattr(RestoredExecutor, "execute", execute)
    counts = {"tool_executions": 0}
    actions = []
    request = {"action": {"command": "append"}}
    with validation.recording_boundary(session, counts, actions, fault="interrupt"):
        assert RestoredExecutor.execute(executor, request).output == b"actual"
        with pytest.raises(KeyboardInterrupt):
            RestoredExecutor.execute(executor, request)
    assert requests == [request] and counts["tool_executions"] == 1
    assert actions[0]["container_id"] == "actual-container"


def test_branch_evidence_checks_actual_model_request_and_tool_returns(recordable):
    session, decision = recordable.session, recordable.decision
    node = record_resume_step(session, decision)
    public = session.report["decision"]["request"]
    nodes, records, context = validation.branch_evidence(session.output / "record", decision,
                                                        public, complete=True)
    assert nodes == (node,) and len(context["messages"]) == 45
    assert records[1]["output"] == "marker\n" and records[1]["exit_code"] == 0
    with pytest.raises(AssertionError):
        validation.branch_evidence(session.output / "record", decision,
                                   {**public, "model": "wrong"}, complete=True)


def test_runner_cannot_write_under_any_fixed_source(tmp_path, monkeypatch):
    source = tmp_path / "source/record"
    source.mkdir(parents=True)
    spec = validation.SourceCase("fixture", source, "run", "node", "state", 0, 2)
    monkeypatch.setattr(validation, "SOURCES", (spec,))
    output = source.parent / "acceptance"
    with pytest.raises(RecordUsageError, match="outside"):
        validation.main(output, tmp_path / "services.json")
    assert not output.exists()
