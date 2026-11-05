from copy import deepcopy

from palinreplay.core.models import Node, Run, RunEnd, Step
from palinreplay.resume.record import _claim_decision, _execute_and_save_group
from palinreplay.resume.record_context import ResumeRecordContext
from palinreplay.resume.recording import begin_recording_step, finish_recording, open_recording
from palinreplay.sandbox.base import ExecResult
from palinreplay.storage.store import RecordStore
from tests.resume import test_record_step

session = test_record_step.session
llm_config = test_record_step.llm_config
response = test_record_step.response
execution_config = test_record_step.execution_config
recordable = test_record_step.recordable


def following_evidence(saved, messages, number):
    """An independently supplied controlled answer for the private recording path."""
    evidence = deepcopy(saved)
    evidence["messages"] = deepcopy(messages)
    evidence["request"]["messages"] = deepcopy(messages)
    for index, action in enumerate(evidence["actions"]):
        action["tool_call_id"] = f"round-{number}-tool-{index}"
        evidence["assistant_message"]["tool_calls"][index]["id"] = action["tool_call_id"]
    evidence["raw_response"]["choices"][0]["message"] = deepcopy(evidence["assistant_message"])
    return evidence


def test_two_groups_share_one_recording_and_one_run_end(recordable):
    case = recordable
    case.session.executor.execute.side_effect = [
        ExecResult("tool", f"round-{number}\n".encode(), 0, .1)
        for number in (1, 1, 2, 2)
    ]
    with _claim_decision(case.session, case.decision) as saved:
        context = ResumeRecordContext(case.session, saved)
        with open_recording(case.session, context.export) as (recorder, step):
            context.accept_decision(step, saved)
            first, _ = _execute_and_save_group(case.session, recorder, step, context, saved["actions"])
            evidence = following_evidence(saved, context.messages, 2)
            step = begin_recording_step(case.session, recorder)
            assert case.session._record_attempt["step_id"] == step.step.step_id
            assert step.step.parent_node_id == first.node_id
            context.accept_decision(step, evidence)
            second, _ = _execute_and_save_group(case.session, recorder, step, context, evidence["actions"])
            assert recorder.store.list(Node) == (first, second)
            assert not recorder.store.list(RunEnd)
            assert len(recorder.store.list(Run)) == 1
            assert second.parent_node_id == first.node_id
            assert [node.index for node in (first, second)] == [
                case.session.start.node.index + 1, case.session.start.node.index + 2,
            ]
            recorder.pause("controlled two-step budget")
        finish_recording(case.session)
    with RecordStore.open(case.session.output / "record") as store:
        assert len(store.list(Step)) == 2
        end, = store.list(RunEnd)
        assert end.status == "paused" and end.last_node_id == second.node_id
        assert store.check_integrity().ok
    assert case.session._record_attempt["complete_nodes"] == 2
    assert len(case.backend.captures) == 2
    assert case.session.executor.execute.call_count == 4
    case.session.executor.close.assert_not_called()
