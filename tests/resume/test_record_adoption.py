import json
from copy import deepcopy

import pytest

from palinreplay.core.models import Node
from palinreplay.record.calls import read_call
from palinreplay.resume.record import _claim_decision, _execute_tools
from palinreplay.resume.record_context import ResumeRecordContext
from palinreplay.sandbox.base import ExecResult
from palinreplay.storage.store import RecordStore
from tests.resume import test_record_context, test_record_tools

session = test_record_tools.session
llm_config = test_record_tools.llm_config
response = test_record_tools.response
decision = test_record_tools.decision
execution_config = test_record_tools.execution_config


@pytest.mark.parametrize("decision,guided", [(None, False), ("one guide", True)],
                         indirect=["decision"])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_real_results_use_mini_messages_and_local_adoption_positions(
    tmp_path, session, decision, execution_config, guided, exit_code,
):
    session.prefix.config["model"]["observation_template"] = "{{output.returncode}}:{{output.output}}"
    inherited = deepcopy(session.prefix.context)
    session.executor.execute.side_effect = [
        ExecResult("first", b"", 0, .1), ExecResult("second", b"marker\xff\r\n", exit_code, .1),
    ]
    with _claim_decision(session, decision) as saved:
        context = ResumeRecordContext(session, saved)
        with RecordStore.create(tmp_path / "record") as store:
            rec = test_record_context.context_recorder(store, context)
            rec.start()
            step = rec.begin_step()
            context.accept_decision(step, saved)
            calls, results = _execute_tools(session, step, saved["actions"])
            context.accept_results(saved["actions"], calls, results)
            assert len(context.messages) == 45 + guided
            assert context.messages[:42] == inherited["messages"]
            if guided:
                assert context.messages[42] == {"role": "user", "content": "one guide"}
            messages = context.messages[-2:]
            assert [m["tool_call_id"] for m in messages] == [a["tool_call_id"] for a in decision.actions]
            assert messages[1]["content"] == f"{exit_code}:marker�\n"
            assert messages[1]["extra"]["returncode"] == exit_code
            position = json.loads(context.export(None).input_position.data)
            assert position["next_message"] == 45 + guided
            assert [entry["start"] for entry in position["adoptions"]] == [42 + guided, 43 + guided, 44 + guided]
            assert [entry["stop"] for entry in position["adoptions"]] == [43 + guided, 44 + guided, 45 + guided]
            for call, message in zip(calls, messages, strict=True):
                trace = read_call(store, call.execution_id)
                assert trace.phase == "accepted"
                adopted = store.read_json(trace.events[-1].payload)
                assert adopted["messages"] == [message]
            rec.commit_step()
            assert len(store.list(Node)) == 2  # ordinary fixture C0 plus complete tool step
            rec.finish()
            rec.close()
    assert session.prefix.context == inherited


@pytest.mark.parametrize("fault", ["missing", "swapped"])
def test_partial_or_misaligned_results_are_not_adopted(
    tmp_path, session, decision, execution_config, fault,
):
    session.prefix.config["model"]["observation_template"] = "{{output.output}}"
    session.executor.execute.side_effect = [ExecResult("first", b"one", 0, 0), ExecResult("second", b"two", 0, 0)]
    with _claim_decision(session, decision) as saved:
        context = ResumeRecordContext(session, saved)
        with RecordStore.create(tmp_path / "record") as store:
            rec = test_record_context.context_recorder(store, context)
            rec.start()
            step = rec.begin_step()
            context.accept_decision(step, saved)
            calls, results = _execute_tools(session, step, saved["actions"])
            before = deepcopy(context.messages)
            with pytest.raises(ValueError, match=r"incomplete|align"):
                context.accept_results(saved["actions"], calls[::-1] if fault == "swapped" else calls,
                                       results[:1] if fault == "missing" else results)
            assert context.messages == before
            assert all(read_call(store, call.execution_id).phase == "result" for call in calls)
            rec.cancel()
            rec.close()


def test_native_submission_stops_a_multi_tool_group_after_recording_the_return(
    tmp_path, session, decision, execution_config,
):
    session.executor.execute.return_value = ExecResult(
        "first", b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\ndone\n", 0, .1,
    )
    with _claim_decision(session, decision) as saved:
        context = ResumeRecordContext(session, saved)
        with RecordStore.create(tmp_path / "record") as store:
            rec = test_record_context.context_recorder(store, context)
            rec.start()
            step = rec.begin_step()
            context.accept_decision(step, saved)
            with pytest.raises(ValueError, match="submission short-circuited"):
                _execute_tools(session, step, saved["actions"])
            assert session.executor.execute.call_count == 1
            rec.cancel()
            rec.close()
