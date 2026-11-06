from copy import deepcopy

import pytest

from palinreplay.agents.mini import load_native
from palinreplay.core.models import RunEnd
from palinreplay.record.calls import read_call
from palinreplay.resume import run_resume_steps
from palinreplay.sandbox.base import ExecResult
from palinreplay.storage.store import RecordStore
from tests.resume import test_run_steps
from tests.resume.test_multiturn_requests import api_messages

session = test_run_steps.session
llm_config = test_run_steps.llm_config
execution_config = test_run_steps.execution_config
response = test_run_steps.response
recordable = test_run_steps.recordable


def test_resume_and_record_share_the_native_control_loop():
    from palinreplay.resume.native import ResumeAgent

    native = load_native()
    assert ResumeAgent.__bases__ == native.RecordingAgent.__bases__ == (native.DefaultAgent,)
    assert ResumeAgent.run is native.DefaultAgent.run
    assert ResumeAgent.query is native.DefaultAgent.query
    assert ResumeAgent.execute_actions is native.DefaultAgent.execute_actions


@pytest.mark.parametrize("recordable", [{"tools": 1, "max_steps": 100,
                                        "instruction": "Inspect B first"}], indirect=True)
def test_text_summary_is_corrected_then_native_submission_stops(recordable, llm_config):
    case = recordable
    prefix = deepcopy(case.session.prefix.context)
    summary = test_run_steps.model_response(2, [])
    summary.choices[0].finish_reason = "stop"
    summary.choices[0].message.content = "B is running; its stdout and stderr go to /dev/null."
    case.complete.side_effect = [summary, test_run_steps.model_response(3, ["submit"])]
    case.session.executor.execute.side_effect = [
        ExecResult("first", b"B status: running\n", 0, .1),
        ExecResult("submit", b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\ndone\n", 0, .1),
    ]
    nodes = run_resume_steps(case.session, case.decision, llm_config=llm_config)
    assert len(nodes) == case.complete.call_count == 3
    assert case.session.executor.execute.call_count == 2
    assert case.session.prefix.context == prefix
    report = case.session.report["recording"]
    assert report["stop_reason"] == "submitted"
    assert report["decisions"] == 2 and report["format_errors"] == 1
    assert report["remaining_steps"] == 97
    with RecordStore.open(case.session.output / "record", read_only=True) as store:
        contexts = [store.read_json(node.context) for node in nodes]
        assert [context["counters"]["n_calls"] for context in contexts] == [21, 22, 23]
        assert [context["counters"]["consecutive_format_errors"] for context in contexts] == [0, 1, 1]
        assert not nodes[1].tool_execution_ids
        for index in (1, 2):
            sent = case.complete.call_args_list[index].args[0]["messages"]
            assert sent == api_messages(contexts[index - 1]["messages"])
            assert sent[:42] == api_messages(prefix["messages"])
            assert sum(message.get("content") == "Inspect B first" for message in sent) == 1
        feedback = contexts[1]["messages"][-1]
        assert feedback["role"] == "user" and "No tool calls found" in feedback["content"]
        assert feedback["extra"]["response"] == summary.model_dump(mode="json")
        trace = read_call(store, nodes[1].model_execution_id)
        assert trace.phase == "accepted"
        assert store.list(RunEnd)[0].status == "completed"
        assert store.check_integrity().ok


@pytest.mark.parametrize("recordable", [{"tools": 1, "max_steps": 2}], indirect=True)
def test_format_correction_cannot_exceed_the_new_request_budget(recordable, llm_config):
    case = recordable
    case.complete.return_value = test_run_steps.model_response(2, [])
    nodes = run_resume_steps(case.session, case.decision, llm_config=llm_config)
    assert len(nodes) == case.complete.call_count == 2
    assert case.session.executor.execute.call_count == 1
    report = case.session.report["recording"]
    assert report["stop_reason"] == "budget_exhausted" and report["remaining_steps"] == 0
    with RecordStore.open(case.session.output / "record", read_only=True) as store:
        end, = store.list(RunEnd)
        assert end.status == "paused"
        assert store.read_json(end.details)["agent_exit_status"] == "LimitsExceeded"
        assert not nodes[-1].tool_execution_ids and store.check_integrity().ok
