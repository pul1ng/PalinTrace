import json

import pytest

from palinreplay.agents.mini import load_native
from palinreplay.core.errors import RecordUsageError
from palinreplay.core.models import Execution, Failure, Node, Operation, RunEnd
from palinreplay.record.calls import RecordingError, read_call
from palinreplay.resume import run_resume_steps
from palinreplay.sandbox.base import ExecResult
from palinreplay.storage.store import RecordStore
from tests.resume import test_run_steps

session = test_run_steps.session
llm_config = test_run_steps.llm_config
execution_config = test_run_steps.execution_config
response = test_run_steps.response
recordable = test_run_steps.recordable


@pytest.mark.parametrize("recordable", [{"tools": 1, "max_steps": 4}], indirect=True)
def test_text_only_second_answer_preserves_its_diagnostic(recordable, llm_config):
    case = recordable
    reply = test_run_steps.model_response(2, [])
    reply.choices[0].finish_reason = "stop"
    reply.choices[0].message.content = "Finished the task."
    case.complete.return_value = reply
    with pytest.raises(load_native().FormatError, match="No tool calls found") as caught:
        run_resume_steps(case.session, case.decision, llm_config=llm_config)
    diagnosis = caught.value.messages[0]["content"]
    report = json.loads(case.session.report_path.read_text())["recording"]
    assert report["error"]["error"] == diagnosis
    assert report["stop_reason"] == "failed" and report["failure_stage"] == "model"
    assert report["complete_nodes"] == case.session.executor.execute.call_count == 1
    assert case.complete.call_count == 2
    with RecordStore.open(case.session.output / "record", read_only=True) as store:
        end, = store.list(RunEnd)
        failure, = store.list(Failure)
        assert store.read_json(end.details)["error"]["message"] == diagnosis
        assert store.read_json(failure.details)["error"]["message"] == diagnosis


@pytest.mark.parametrize("recordable", [{"tools": 1, "max_steps": 4}], indirect=True)
@pytest.mark.parametrize("fault", ["model", "model_interrupt", "tool", "tool_interrupt",
                                   "snapshot", "after_publication"])
def test_second_round_failure_preserves_published_progress_and_stops(
    recordable, llm_config, monkeypatch, fault,
):
    case = recordable
    interrupted = fault.endswith("interrupt")
    error = KeyboardInterrupt("user interruption") if interrupted else RuntimeError(fault)
    expected = KeyboardInterrupt if interrupted else (
        RecordingError if fault in ("snapshot", "after_publication") else RuntimeError
    )
    case.complete.return_value = test_run_steps.model_response(2, ["second tool"])
    if fault.startswith("model"):
        case.complete.side_effect = error
    case.session.executor.execute.side_effect = [
        ExecResult("first", b"first marker\n", 0, .1),
        error if fault.startswith("tool") else ExecResult("second", b"second marker\n", 0, .1),
    ]
    if fault == "snapshot":
        original = case.backend.capture

        def capture(store, boundary, previous):
            if boundary.index == 2:
                raise error
            return original(store, boundary, previous)

        case.backend.capture = capture
    elif fault == "after_publication":
        original = RecordStore.commit_node

        def commit(store, node):
            result = original(store, node)
            if store.root == case.session.output / "record" and node.index == 2:
                raise error
            return result

        monkeypatch.setattr(RecordStore, "commit_node", commit)
    with pytest.raises(expected):
        run_resume_steps(case.session, case.decision, llm_config=llm_config)
    attempt = case.session.report["recording"]
    assert attempt["status"] == ("interrupted" if interrupted else "failed")
    assert attempt["stop_reason"] == attempt["status"]
    assert attempt["total_model_requests"] == 2
    assert attempt["complete_nodes"] == (2 if fault == "after_publication" else 1)
    assert attempt["stage"] == ("model" if fault.startswith("model") else
                                "tools" if fault.startswith("tool") else "commit")
    assert case.complete.call_count == 2  # no third request after the failed round
    assert case.session.executor.execute.call_count == (1 if fault.startswith("model") else 2)
    persisted = json.loads(case.session.report_path.read_text())["recording"]
    assert persisted["complete_nodes"] == attempt["complete_nodes"]
    with RecordStore.open(case.session.output / "record") as store:
        nodes = store.list(Node)
        assert len(nodes) == attempt["complete_nodes"]
        assert attempt["committed_node_ids"] == [node.node_id for node in nodes]
        assert attempt["node_id"] == nodes[-1].node_id
        assert store.get_node(nodes[0].node_id) == nodes[0]
        assert store.read_json(nodes[0].context)["messages"][-1]["content"].endswith("first marker\n")
        first = read_call(store, nodes[0].tool_execution_ids[0])
        assert store.read_bytes(first.events[1].payload) == b"first marker\n"
        models = [op for op in store.list(Operation) if op.kind == "model"]
        executions = {e.operation_id: e.execution_id for e in store.list(Execution)}
        second = read_call(store, executions[models[-1].operation_id])
        assert len(store.read_json(second.execution.request)["messages"]) == 44
        assert second.phase == ("unknown" if fault.startswith("model") else "accepted")
        assert store.list(Failure)
        end, = store.list(RunEnd)
        assert end.status == ("cancelled" if interrupted else "failed")
        assert end.last_node_id == nodes[-1].node_id
        assert store.check_integrity().ok
    with pytest.raises(RecordUsageError, match="already consumed"):
        run_resume_steps(case.session, case.decision, llm_config=llm_config)
    assert case.complete.call_count == 2
