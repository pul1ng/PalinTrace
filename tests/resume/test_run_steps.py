import json
import shlex
import subprocess

import pytest

from palinreplay.core.errors import RecordUsageError
from palinreplay.core.models import Execution, Node, Operation, RunEnd, Step, ToolGroup
from palinreplay.record.calls import read_call
from palinreplay.resume import record_resume_step, run_resume_steps
from palinreplay.sandbox.base import ExecResult
from palinreplay.storage.store import RecordStore
from tests.resume import test_record_step

session = test_record_step.session
llm_config = test_record_step.llm_config
execution_config = test_record_step.execution_config
recordable = test_record_step.recordable


def model_response(number, commands):
    from litellm import ModelResponse

    return ModelResponse(id=f"round-{number}", model="M", created=1, choices=[{
        "index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": f"decision {number}",
            "tool_calls": [{"id": f"round-{number}-tool-{index}", "type": "function",
                            "function": {"name": "bash", "arguments": json.dumps({"command": command})}}
                           for index, command in enumerate(commands)],
        },
    }], usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


def marker_command(path, number):
    return f"printf 'round-{number}\\n' >> {shlex.quote(str(path))}; cat {shlex.quote(str(path))}"


@pytest.fixture
def response(tmp_path):
    return model_response(1, [marker_command(tmp_path / "marker", 1)])


@pytest.mark.parametrize("recordable,budget", [({"tools": 1, "max_steps": 1}, 1),
                                             ({"tools": 1, "max_steps": 2}, 2)],
                         indirect=["recordable"])
def test_public_runner_counts_first_decision_and_keeps_workspace(
    recordable, llm_config, tmp_path, budget,
):
    case = recordable
    requests = []

    def execute(request):
        requests.append(request)
        result = subprocess.run([*request["interpreter"], request["action"]["command"]],
                                cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return ExecResult(request["action"]["command"], result.stdout, result.returncode, .1)

    case.session.executor.execute.side_effect = execute
    case.complete.return_value = model_response(2, [marker_command(tmp_path / "marker", 2)])
    nodes = run_resume_steps(case.session, case.decision, llm_config=llm_config)
    assert [node.index for node in nodes] == list(range(1, budget + 1))
    assert case.complete.call_count == len(requests) == budget
    assert len(case.backend.captures) == budget
    assert (tmp_path / "marker").read_text().splitlines() == [f"round-{n}" for n in range(1, budget + 1)]
    with RecordStore.open(case.session.output / "record") as store:
        assert store.list(Node) == tuple(nodes)
        assert len(store.list(Step)) == budget
        end, = store.list(RunEnd)
        assert end.status == "paused" and end.last_node_id == nodes[-1].node_id
        assert store.read_json(end.details)["reason"] == "Resume decision budget exhausted"
        assert store.check_integrity().ok
        if budget == 2:
            assert nodes[1].parent_node_id == nodes[0].node_id
            first = store.read_json(nodes[0].context)["messages"]
            second = store.read_json(nodes[1].context)["messages"]
            assert first[-1]["content"].endswith("round-1\n")
            assert second[-1]["content"].endswith("round-1\nround-2\n")
            assert case.complete.call_args.args[0]["messages"][-1]["content"].endswith("round-1\n")
    for consume in (run_resume_steps, record_resume_step):
        with pytest.raises(RecordUsageError, match="already consumed"):
            consume(case.session, case.decision)
    assert case.session.executor.execute.call_count == budget
    case.session.executor.close.assert_not_called()


@pytest.mark.parametrize("recordable", [{"tools": 1, "max_steps": 2}], indirect=True)
def test_ordinary_nonzero_return_reaches_the_next_model_request(recordable, llm_config):
    case = recordable
    case.session.executor.execute.side_effect = [
        ExecResult("first", b"ordinary failure\n", 7, .1),
        ExecResult("second", b"recovered\n", 0, .1),
    ]
    case.complete.return_value = model_response(2, ["repair"])
    nodes = run_resume_steps(case.session, case.decision, llm_config=llm_config)
    assert len(nodes) == case.complete.call_count == 2
    messages = case.complete.call_args.args[0]["messages"]
    assert messages[-1]["tool_call_id"] == case.decision.actions[0]["tool_call_id"]
    assert messages[-1]["content"] == "openai/M:7:ordinary failure\n"
    with RecordStore.open(case.session.output / "record") as store:
        trace = read_call(store, nodes[0].tool_execution_ids[0])
        result = next(event for event in trace.events if event.kind == "result")
        assert result.outcome == "error"
        assert store.read_json(result.details)["backend"]["decoded"]["returncode"] == 7
        assert store.list(RunEnd)[0].status == "paused"
    assert case.session.report["recording"]["stop_reason"] == "budget_exhausted"


@pytest.mark.parametrize("recordable", [{"tools": 1, "max_steps": 5}], indirect=True)
@pytest.mark.parametrize("submit_round", [1, 2])
def test_single_tool_submission_commits_and_stops_before_budget(
    recordable, llm_config, submit_round,
):
    case = recordable
    case.complete.return_value = model_response(2, ["submit"])
    case.session.executor.execute.side_effect = [ExecResult("ordinary", b"progress\n", 0, .1)] * (
        submit_round - 1
    ) + [ExecResult("submit", b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\ndone\n", 0, .1)]
    nodes = run_resume_steps(case.session, case.decision, llm_config=llm_config)
    assert len(nodes) == case.complete.call_count == submit_round
    assert case.session.executor.execute.call_count == submit_round
    with RecordStore.open(case.session.output / "record") as store:
        end, = store.list(RunEnd)
        assert end.status == "completed" and end.last_node_id == nodes[-1].node_id
        details = store.read_json(end.details)
        assert details["agent_exit_status"] == "Submitted" and details["record_complete"]
        assert store.read_json(nodes[-1].context)["messages"][-1]["role"] == "exit"
        assert store.check_integrity().ok
    assert case.session.report["recording"]["stop_reason"] == "submitted"


@pytest.mark.parametrize("recordable", [{"tools": 1, "max_steps": 5}], indirect=True)
@pytest.mark.parametrize("submit_index", [0, 2])
def test_multi_tool_submission_keeps_previous_node_and_actual_calls(
    recordable, llm_config, submit_index,
):
    case = recordable
    case.complete.return_value = model_response(2, ["one", "two", "three"])
    case.session.executor.execute.side_effect = [ExecResult("first-round", b"progress\n", 0, .1)] + [
        ExecResult("ordinary", b"ordinary output\n", 0, .1)
    ] * submit_index + [ExecResult("submit", b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\ndone\n", 0, .1)]
    nodes = run_resume_steps(case.session, case.decision, llm_config=llm_config)
    assert len(nodes) == 1 and case.complete.call_count == 2
    assert case.session.executor.execute.call_count == submit_index + 2
    with RecordStore.open(case.session.output / "record") as store:
        end, = store.list(RunEnd)
        assert end.status == "incomplete" and end.last_node_id == nodes[0].node_id
        details = store.read_json(end.details)
        assert details["agent_exit_status"] == "Submitted" and not details["record_complete"]
        group = store.list(ToolGroup)[-1]
        assert len(group.tool_call_ids) == 3
        tools = [op for op in store.list(Operation) if op.step_id == group.step_id and op.kind == "tool"]
        assert [op.tool_call_id for op in tools] == list(group.tool_call_ids[:submit_index + 1])
        executions = {e.operation_id: e.execution_id for e in store.list(Execution)}
        for index, tool in enumerate(tools):
            trace = read_call(store, executions[tool.operation_id])
            assert trace.phase == ("accepted" if index == submit_index else "result")
        assert store.list(Node) == tuple(nodes) and store.check_integrity().ok
    assert case.session.report["recording"]["status"] == "incomplete"
    assert case.session.report["recording"]["stop_reason"] == "incomplete_tool_group"
