import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from palinreplay.core.errors import RecordUsageError
from palinreplay.core.models import Execution, Failure, Node, Operation
from palinreplay.record.calls import RecordingError, read_call
from palinreplay.resume import record_resume_step
from palinreplay.resume.report import finish_report
from palinreplay.sandbox.base import ExecResult
from palinreplay.storage.store import RecordStore
from tests.resume import test_record_step

session = test_record_step.session
llm_config = test_record_step.llm_config
response = test_record_step.response
execution_config = test_record_step.execution_config
recordable = test_record_step.recordable


@pytest.mark.parametrize("fault", ["interrupt", "adoption", "snapshot"])
def test_incomplete_round_keeps_real_results_and_cannot_replay(recordable, monkeypatch, fault):
    case = recordable
    original = KeyboardInterrupt("second interrupted") if fault == "interrupt" else RuntimeError(fault)
    saved = deepcopy(case.session.report["decision"])
    expected = KeyboardInterrupt if fault == "interrupt" else RecordingError if fault == "snapshot" else RuntimeError
    if fault == "interrupt":
        case.session.executor.execute.side_effect = [ExecResult("first", b"marker\n", 0, .1), original]
    elif fault == "adoption":
        monkeypatch.setattr("palinreplay.resume.record_context.ResumeRecordContext.accept_results",
                            Mock(side_effect=original))
    else:
        case.backend.capture = Mock(side_effect=original)
    with pytest.raises(expected):
        record_resume_step(case.session, case.decision)
    assert case.session.report["decision"] == saved
    attempt = case.session.report["recording"]
    assert attempt["status"] == ("interrupted" if fault == "interrupt" else "failed")
    assert attempt["complete_nodes"] == 0
    assert attempt["tool_results"] == (1 if fault == "interrupt" else 2)
    assert attempt["unknown_tool_results"] == (1 if fault == "interrupt" else 0)
    with RecordStore.open(case.session.output / "record") as store:
        assert not store.list(Node) and store.list(Failure)
        tools = {op.operation_id for op in store.list(Operation) if op.kind == "tool"}
        executions = [e for e in store.list(Execution) if e.operation_id in tools]
        first = read_call(store, executions[0].execution_id)
        assert store.read_bytes(first.events[1].payload) == (b"marker\n" if fault == "interrupt" else b"")
        assert store.check_integrity().ok
    before = case.session.executor.execute.call_count
    with pytest.raises(RecordUsageError, match="already consumed"):
        record_resume_step(case.session, case.decision)
    assert case.session.executor.execute.call_count == before
    # A caller catching the failure must not make the managed-session outcome pass.
    report = case.session.report
    report.update(cleanup_errors=[], cleanup={}, caller={"status": "completed"})
    finish_report(report, case.session.output, restored=None, executor=case.session.executor,
                  client=None, own_client=False, primary=None)
    persisted = json.loads(case.session.report_path.read_text())
    assert persisted["status"] == "failed" and persisted["caller"]["status"] == "completed"
    assert persisted["decision"]["status"] == "succeeded" and persisted["cleanup_confirmed"]


def test_error_after_store_publication_reports_the_real_committed_node(recordable, monkeypatch):
    real = RecordStore.commit_node
    def publish_then_fail(store, node):
        real(store, node)
        raise OSError("after publication")
    monkeypatch.setattr(RecordStore, "commit_node", publish_then_fail)
    with pytest.raises(RecordingError):
        record_resume_step(recordable.session, recordable.decision)
    attempt = recordable.session.report["recording"]
    assert attempt["status"] == "failed" and attempt["complete_nodes"] == 1
    with RecordStore.open(recordable.session.output / "record") as store:
        node, = store.list(Node)
        assert attempt["node_id"] == node.node_id
        assert attempt["committed_node_ids"] == [node.node_id]
        assert store.get_node(node.node_id) == node


def test_unreadable_publication_facts_are_unknown_not_zero(recordable, monkeypatch):
    real = RecordStore.open
    def unavailable(path, **kwargs):
        if str(path) == str(recordable.session.output / "record"):
            raise OSError("catalog temporarily unreadable")
        return real(path, **kwargs)
    monkeypatch.setattr(RecordStore, "open", unavailable)
    with pytest.raises(OSError, match="catalog temporarily unreadable"):
        record_resume_step(recordable.session, recordable.decision)
    attempt = recordable.session.report["recording"]
    assert attempt["status"] == "failed" and attempt["complete_nodes"] is None
    assert attempt["progress_error"]["error_type"] == "OSError"
    assert attempt["stop_reason"] == "failed" and attempt["failure_stage"] == "progress"
    with real(recordable.session.output / "record") as store:
        assert len(store.list(Node)) == 1


def test_report_failure_after_commit_preserves_node_and_marks_failure(recordable, monkeypatch):
    from palinreplay.resume import recording

    real = recording.write_json
    def fail_success(path, data, **kwargs):
        if data.get("recording", {}).get("status") == "succeeded":
            raise OSError("report full")
        return real(path, data, **kwargs)
    monkeypatch.setattr(recording, "write_json", fail_success)
    with pytest.raises(OSError, match="report full"):
        record_resume_step(recordable.session, recordable.decision)
    attempt = recordable.session.report["recording"]
    assert attempt["status"] == "failed" and attempt["complete_nodes"] == 1
    assert attempt["report_error"]["error_type"] == "OSError"
    assert attempt["stop_reason"] == "failed" and attempt["failure_stage"] == "report"
    assert recordable.session.report["decision"]["status"] == "succeeded"
