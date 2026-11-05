import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from palinreplay.core.errors import RecordUsageError
from palinreplay.core.interfaces import Payload
from palinreplay.core.models import Execution, Failure, Node, Operation, RunEnd, ToolGroup
from palinreplay.record.calls import read_call
from palinreplay.resume import (
    ResumeSession,
    decide_resume_start,
    record_resume_step,
    select_resume_start,
)
from palinreplay.resume.context import ResumePrefix
from palinreplay.resume.materials import load_resume_materials
from palinreplay.resume.record import _claim_decision, _execute_and_save_group
from palinreplay.resume.record_context import ResumeRecordContext
from palinreplay.resume.recording import open_recording
from palinreplay.resume.report import finish_report
from palinreplay.sandbox.base import ExecResult
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake
from tests.record.test_restore_materials import _record_run
from tests.resume import test_record_tools

session = test_record_tools.session
llm_config = test_record_tools.llm_config
response = test_record_tools.response
execution_config = test_record_tools.execution_config


@pytest.fixture
def recordable(tmp_path, session, llm_config, response, execution_config, monkeypatch, request):
    monkeypatch.setattr("palinreplay.storage.btrfs.verification.verify_state", lambda *a: None)
    execution_config["interpreter"] = ["bash", "-c"]
    context = deepcopy(session.prefix.context)
    context["config"].setdefault("agent", {})
    context["counters"]["consecutive_format_errors"] = 0
    for key in ("extra_template_vars", "template_vars", "clock", "global_stats", "model_accounting"):
        context.setdefault(key, {})
    context["template_vars"]["model_name"] = context["config"]["model"]["model_name"]
    context["config"]["model"]["observation_template"] = (
        "{{model_name}}:{{output.returncode}}:{{output.output}}"
    )
    source = tmp_path / "source-record"
    with RecordStore.create(source) as store:
        _, node, parent_next, *_ = _record_run(store, "parent", payloads={
            "config": Payload.json(context["config"]), "context": Payload.json(context),
            "input_position": Payload.json({"next_message": 42, "adoptions": []}),
        })
    start = select_resume_start(source, node.node_id, model="M", max_steps=1)
    materials = load_resume_materials(start)
    output = tmp_path / "branch"
    output.mkdir()
    executor = Mock(state="running")
    executor.execute.side_effect = [ExecResult("first", b"", 0, .1), ExecResult("second", b"marker\n", 0, .1)]
    session = ResumeSession(start=start, materials=materials, restored=SimpleNamespace(),
                            executor=executor, prefix=ResumePrefix(materials), output=output,
                            report={"phase": "caller", "restore_status": "passed",
                                    "preparation": {"status": "skipped"}})
    options = getattr(request, "param", 2)
    tools = options["tools"] if isinstance(options, dict) else options
    instruction = options.get("instruction") if isinstance(options, dict) else None
    for index in range(2, tools + 1):
        extra = response.choices[0].message.tool_calls[0].model_copy(deep=True)
        extra.id = f"branch-tool-{index}"
        extra.function.arguments = '{"command":"cat /tmp/marker"}'
        response.choices[0].message.tool_calls.append(extra)
    complete = Mock(return_value=response)
    monkeypatch.setattr("palinreplay.resume.decision.complete_decision_request", complete)
    decision = decide_resume_start(session, instruction=instruction, llm_config=llm_config)
    backend = fake.FakeStateBackend()
    monkeypatch.setattr("palinreplay.resume.recording.BtrfsStateBackend", lambda *a, **kw: backend)
    return SimpleNamespace(session=session, decision=decision, backend=backend,
                           complete=complete, parent_next=parent_next)


def test_group_commit_leaves_recording_open_for_its_caller(recordable):
    case = recordable
    with _claim_decision(case.session, case.decision) as saved:
        context = ResumeRecordContext(case.session, saved)
        with open_recording(case.session, context.export) as (recorder, step):
            context.accept_decision(step, saved)
            node, details = _execute_and_save_group(
                case.session, recorder, step, context, saved["actions"],
            )
            assert node.index == case.session.start.node.index + 1
            assert details is None
            assert recorder.phase == "ready" and recorder.end is None
            assert recorder.store.list(Node) == (node,)
            assert not recorder.store.list(RunEnd)
            assert case.session.executor.execute.call_count == 2
            assert case.complete.call_count == 1
            recorder.finish()
            assert len(recorder.store.list(RunEnd)) == 1


def test_public_entry_commits_one_independent_node_and_does_not_query_again(recordable):
    case = recordable
    node = record_resume_step(case.session, case.decision)
    assert node.index == case.session.start.node.index + 1
    assert len(case.backend.captures) == 1
    assert case.complete.call_count == 1
    assert case.session.executor.execute.call_count == 2
    assert case.session._record_attempt["status"] == "succeeded"
    assert case.session._record_attempt["complete_nodes"] == 1
    assert case.session._record_attempt["message_count"] == 45
    with RecordStore.open(case.session.output / "record") as store:
        assert store.list(Node) == (node,)
        assert store.get_node(node.node_id) == node
        assert len(node.tool_execution_ids) == 2
        assert store.read_json(node.input_position)["next_message"] == 45
        assert store.check_integrity().ok
        assert store.list(RunEnd)[0].status == "completed"
    with RecordStore.open(case.session.start.source_store) as source:
        assert source.get_node(case.parent_next.node_id) == case.parent_next
    assert node.node_id != case.parent_next.node_id and node.run_id != case.parent_next.run_id
    case.session.executor.close.assert_not_called()


def test_public_entry_rejects_duplicate_without_new_tools_or_nodes(recordable):
    case = recordable
    node = record_resume_step(case.session, case.decision)
    with pytest.raises(RecordUsageError, match="already consumed"):
        record_resume_step(case.session, case.decision)
    assert case.session.executor.execute.call_count == 2
    assert len(case.backend.captures) == 1
    with RecordStore.open(case.session.output / "record") as store:
        assert store.list(Node) == (node,)


def test_tool_observation_uses_the_selected_branch_model(recordable):
    session = recordable.session
    assert session.prefix.context["template_vars"]["model_name"] == "parent-A"
    node = record_resume_step(session, recordable.decision)
    with RecordStore.open(session.output / "record") as store:
        context = store.read_json(node.context)
        assert context["config"]["model"]["model_name"] == "openai/M"
        assert context["template_vars"]["model_name"] == "openai/M"
        assert context["messages"][-1]["content"] == "openai/M:0:marker\n"
        trace = read_call(store, node.tool_execution_ids[-1])
        assert store.read_json(trace.events[-1].payload)["messages"] == [context["messages"][-1]]
        assert store.read_bytes(trace.events[1].payload) == b"marker\n"
    assert session.prefix.context["template_vars"]["model_name"] == "parent-A"


@pytest.mark.parametrize("recordable,submit_index", [(2, 0), (2, 1), (1, 0)], indirect=["recordable"])
def test_native_submission_adopts_only_the_submitting_tool(recordable, submit_index):
    session, decision = recordable.session, recordable.decision
    actions = decision.actions
    outputs = [b"ordinary output\n"] * submit_index + [
        b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\ndone\n"
    ]
    session.executor.execute.side_effect = [
        ExecResult(action["command"], output, 0, .1)
        for action, output in zip(actions, outputs, strict=False)
    ]
    exit_message = {
        "role": "exit", "content": "done\n", "extra": {"exit_status": "Submitted", "submission": "done\n"},
    }
    node = record_resume_step(session, decision)
    complete = len(actions) == 1
    assert (node is not None) == complete
    assert session.executor.execute.call_count == len(outputs)
    assert recordable.complete.call_count == 1
    with pytest.raises(RecordUsageError, match="already consumed"):
        record_resume_step(session, decision)
    with RecordStore.open(session.output / "record") as store:
        end, = store.list(RunEnd)
        assert end.status == ("completed" if complete else "incomplete")
        details = store.read_json(end.details)
        assert details["agent_exit_status"] == "Submitted"
        group, = store.list(ToolGroup)
        assert list(group.tool_call_ids) == [action["tool_call_id"] for action in actions]
        tools = [op for op in store.list(Operation) if op.kind == "tool"]
        assert [op.tool_call_id for op in tools] == list(group.tool_call_ids[:len(outputs)])
        executions = {e.operation_id: e.execution_id for e in store.list(Execution)}
        for index, op in enumerate(tools):
            trace = read_call(store, executions[op.operation_id])
            assert store.read_bytes(trace.events[1].payload) == outputs[index]
            if index == submit_index:
                assert trace.phase == "accepted"
                assert store.read_json(trace.events[-1].payload)["messages"] == [exit_message]
            else:
                assert trace.phase == "result"
        assert not store.list(Failure) and store.check_integrity().ok
        if complete:
            assert store.list(Node) == (node,)
            assert store.read_json(node.context)["messages"][-1] == exit_message
        else:
            assert not store.list(Node) and not recordable.backend.captures
            assert details["final_control_context"]["messages"][-1] == exit_message
            admitted = details["final_input_position"]["adoptions"][-1]
            assert admitted["execution_id"] == executions[tools[-1].operation_id]
    report = session.report
    report.update(cleanup_errors=[], cleanup={}, caller={"status": "completed"})
    finish_report(report, session.output, restored=None, executor=session.executor, client=None,
                  own_client=False, primary=None)
    final = json.loads(session.report_path.read_text())
    assert final["recording"]["status"] == ("succeeded" if complete else "incomplete")
    assert final["recording"]["accepted_tools"] == 1
    assert final["recording"]["complete_nodes"] == int(complete)
    assert final["cleanup_confirmed"] and final["status"] == ("passed" if complete else "incomplete")
