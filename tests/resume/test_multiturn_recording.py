from copy import deepcopy

import pytest

from palinreplay.core.models import Execution, Node, Operation, Run, RunEnd, Step, ToolGroup
from palinreplay.record.calls import read_call
from palinreplay.resume.decision import record_branch_decision
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


@pytest.mark.parametrize("recordable", [2, {"tools": 2, "instruction": "branch-only guide"}],
                         indirect=True)
def test_shared_context_keeps_source_and_each_admitted_round(recordable):
    case = recordable
    source = deepcopy(case.session.context)
    case.session.executor.execute.side_effect = [
        ExecResult("tool", f"round-{number}-tool-{tool}\n".encode(), 0, .1)
        for number in (1, 2) for tool in (1, 2)
    ]
    with _claim_decision(case.session, case.decision) as saved:
        context = ResumeRecordContext(case.session, saved)
        with open_recording(case.session, context.export) as (recorder, step):
            context.accept_decision(step, saved)
            first, _ = _execute_and_save_group(case.session, recorder, step, context, saved["actions"])
            first_messages = deepcopy(context.messages)
            following = following_evidence(saved, context.messages, 2)
            context.account_decision(following["accounting"])
            step = begin_recording_step(case.session, recorder)
            context.accept_decision(step, following)
            second, _ = _execute_and_save_group(case.session, recorder, step, context, following["actions"])
            recorder.pause("controlled two-step budget")
            messages = context.messages
            assert messages[:42] == source["messages"]
            assert messages[:len(first_messages)] == first_messages
            assert sum(m.get("content") == "branch-only guide" for m in messages) == int(
                saved["instruction"] is not None
            )
            tools = [m for m in messages[42:] if m["role"] == "tool"]
            assert [m["tool_call_id"] for m in tools] == [
                a["tool_call_id"] for evidence in (saved, following) for a in evidence["actions"]
            ]
            assert [m["content"].split(":", 2)[-1] for m in tools] == [
                "round-1-tool-1\n", "round-1-tool-2\n", "round-2-tool-1\n", "round-2-tool-2\n",
            ]
            assert context.context["counters"]["n_calls"] == source["counters"]["n_calls"] + 2
            assert context.context["resume"]["recording_model_requests"] == 1
            for node, expected in ((first, first_messages), (second, messages)):
                assert recorder.store.read_json(node.context)["messages"] == expected
                assert recorder.store.read_json(node.input_position)["next_message"] == len(expected)
            assert context.adoptions[-1]["stop"] == len(messages)
            assert recorder.store.check_integrity().ok
    assert case.session.context == source


def test_later_request_and_answer_are_recorded_in_their_own_step(recordable, llm_config, response):
    case = recordable
    saved_first = deepcopy(case.session._decision_evidence)
    second_response = response.model_copy(deep=True)
    second_response.id = "second-live-response"
    for index, tool in enumerate(second_response.choices[0].message.tool_calls):
        tool.id = f"second-live-tool-{index}"
    case.session.executor.execute.side_effect = [ExecResult("tool", b"marker\n", 0, .1)] * 4
    with _claim_decision(case.session, case.decision) as saved:
        context = ResumeRecordContext(case.session, saved)
        with open_recording(case.session, context.export) as (recorder, step):
            context.accept_decision(step, saved)
            first, _ = _execute_and_save_group(case.session, recorder, step, context, saved["actions"])
            expected_messages = deepcopy(context.messages)
            step = begin_recording_step(case.session, recorder)

            def complete(request, credentials):
                operations = [op for op in recorder.store.list(Operation)
                              if op.step_id == step.step.step_id and op.kind == "model"]
                operation, = operations
                execution, = [e for e in recorder.store.list(Execution)
                              if e.operation_id == operation.operation_id]
                assert recorder.store.read_json(execution.request) == request
                assert recorder.store.read_json(execution.context) == expected_messages
                assert read_call(recorder.store, execution.execution_id).phase == "dispatched"
                return second_response

            case.complete.side_effect = complete
            following = record_branch_decision(case.session, step, context, llm_config=llm_config)
            assert following.messages == expected_messages
            second, _ = _execute_and_save_group(case.session, recorder, step, context, following.actions)
            recorder.pause("two model requests")
            assert len(recorder.store.list(ToolGroup)) == 2
            for node, actual_calls in ((first, 0), (second, 1)):
                trace = read_call(recorder.store, node.model_execution_id)
                result = next(event for event in trace.events if event.kind == "result")
                details = recorder.store.read_json(result.details)["backend"]
                assert details["actual_model_calls"] == actual_calls
                assert trace.operation.step_id == node.step_id
            trace = read_call(recorder.store, second.model_execution_id)
            result = next(event for event in trace.events if event.kind == "result")
            assert recorder.store.read_json(result.payload) == following.raw_response
            group = recorder.store.list(ToolGroup)[-1]
            assert group.tool_call_ids == tuple(action["tool_call_id"] for action in following.actions)
            assert group.step_id == second.step_id
            assert recorder.store.check_integrity().ok
        finish_recording(case.session)
    assert case.complete.call_count == 2
    assert case.session._record_attempt["model_requests"] == 1
    assert case.session._record_attempt["complete_nodes"] == 2
    assert case.session._decision_evidence == saved_first


@pytest.mark.parametrize("fault", ["request", "parse"])
def test_second_model_failure_retains_its_request_and_exposed_response(
    recordable, llm_config, response, fault,
):
    case = recordable
    if fault == "request":
        case.complete.side_effect = RuntimeError("second request failed")
    else:
        malformed = response.model_copy(deep=True)
        malformed.choices[0].message.tool_calls[0].function.arguments = "not json"
        case.complete.return_value = malformed
    with pytest.raises(Exception) as failed:
        with _claim_decision(case.session, case.decision) as saved:
            context = ResumeRecordContext(case.session, saved)
            with open_recording(case.session, context.export) as (recorder, step):
                context.accept_decision(step, saved)
                _execute_and_save_group(case.session, recorder, step, context, saved["actions"])
                step = begin_recording_step(case.session, recorder)
                record_branch_decision(case.session, step, context, llm_config=llm_config)
    finish_recording(case.session, failed.value)
    with RecordStore.open(case.session.output / "record") as store:
        assert len(store.list(Node)) == 1
        model = [op for op in store.list(Operation) if op.kind == "model"][-1]
        execution = next(e for e in store.list(Execution) if e.operation_id == model.operation_id)
        trace = read_call(store, execution.execution_id)
        assert len(store.read_json(execution.request)["messages"]) == 45
        result = next(event for event in trace.events if event.kind == "result")
        assert result.outcome == ("unknown" if fault == "request" else "returned")
        if fault == "parse":
            assert store.read_json(result.payload) == malformed.model_dump(mode="json")
        assert len(store.list(ToolGroup)) == 1
        assert store.check_integrity().ok
    assert case.complete.call_count == 2
    assert case.session.executor.execute.call_count == 2
    assert case.session._record_attempt["complete_nodes"] == 1
