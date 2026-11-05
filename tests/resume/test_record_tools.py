from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from palinreplay.core.models import Execution, Node, Operation
from palinreplay.record.calls import read_call
from palinreplay.resume.record import _claim_decision, _execute_tools
from palinreplay.resume.record_context import ResumeRecordContext
from palinreplay.sandbox.base import ExecResult
from palinreplay.storage.store import RecordStore
from tests.resume import test_record_context

session = test_record_context.session
llm_config = test_record_context.llm_config
response = test_record_context.response
decision = test_record_context.decision


@pytest.fixture
def execution_config(session):
    config = {"cwd": "/saved/workdir", "env": {"PAGER": "cat"},
              "interpreter": ["bash", "-lc"], "timeout": 17, "forward_env": []}
    session.materials.config = {"environment": config}
    session.materials.context["config"]["environment"] = config
    session.materials.runtime = SimpleNamespace(launch=SimpleNamespace(shell=("bash", "-lc")))
    return config


@pytest.mark.parametrize("exit_code", [0, 7])
def test_tools_use_saved_execution_defaults_and_persist_before_next_call(
    tmp_path, session, decision, execution_config, exit_code,
):
    requests = []
    with _claim_decision(session, decision) as saved:
        context = ResumeRecordContext(session, saved)
        with RecordStore.create(tmp_path / "record") as store:
            rec = test_record_context.context_recorder(store, context)
            rec.start()
            step = rec.begin_step()
            context.accept_decision(step, saved)
            def execute(request):
                if requests:
                    first = next(op for op in store.list(Operation) if op.kind == "tool")
                    execution = next(e for e in store.list(Execution)
                                     if e.operation_id == first.operation_id)
                    trace = read_call(store, execution.execution_id)
                    assert trace.phase == "result"
                    assert store.read_bytes(trace.events[-1].payload) == b"marker\xff\r\n"
                requests.append(request)
                return ExecResult(request["action"]["command"], b"marker\xff\r\n", exit_code, .1)
            session.executor.execute.side_effect = execute
            calls, results, submission = _execute_tools(session, step, saved["actions"])
            assert submission is None
            assert len(calls) == len(results) == 2
            assert [request["action"] for request in requests] == decision.actions
            for request in requests:
                assert request["cwd"] == execution_config["cwd"]
                assert request["env"] == execution_config["env"]
                assert request["interpreter"] == execution_config["interpreter"]
                assert request["timeout_sec"] == 17
            assert session._record_attempt["tool_executions"] == 2
            for call in calls:
                trace = read_call(store, call.execution_id)
                result = trace.events[-1]
                assert result.kind == "result"
                assert result.outcome == ("error" if exit_code else "returned")
                assert store.read_bytes(result.payload) == b"marker\xff\r\n"
                decoded = store.read_json(result.details)["backend"]["decoded"]
                assert decoded == {"returncode": exit_code, "output": "marker�\n", "exception_info": ""}
            assert len(store.list(Node)) == 1  # only the fixture C0, no completed step
            rec.cancel()
            rec.close()
    assert session.executor.execute.call_count == 2
    session.executor.close.assert_not_called()


def test_second_call_interrupt_keeps_first_real_return(
    tmp_path, session, decision, execution_config,
):
    with _claim_decision(session, decision) as saved:
        context = ResumeRecordContext(session, saved)
        with RecordStore.create(tmp_path / "record") as store:
            rec = test_record_context.context_recorder(store, context)
            rec.start()
            step = rec.begin_step()
            context.accept_decision(step, saved)
            session.executor.execute = Mock(side_effect=[
                ExecResult("first", b"marker\n", 0, .1), KeyboardInterrupt(),
            ])
            with pytest.raises(KeyboardInterrupt):
                _execute_tools(session, step, saved["actions"])
            tools = [op for op in store.list(Operation) if op.kind == "tool"]
            executions = [e for e in store.list(Execution)
                          if e.operation_id in {op.operation_id for op in tools}]
            traces = [read_call(store, e.execution_id) for e in executions]
            assert [trace.phase for trace in traces] == ["result", "unknown"]
            assert store.read_bytes(traces[0].events[-1].payload) == b"marker\n"
            assert len(store.list(Node)) == 1
            rec.close()
    assert session.executor.execute.call_count == 2
