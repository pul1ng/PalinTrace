from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from palinreplay.core.interfaces import Payload
from palinreplay.core.models import Node, RunEnd
from palinreplay.resume import (
    ResumeSession,
    decide_resume_start,
    record_resume_step,
    select_resume_start,
)
from palinreplay.resume.context import ResumePrefix
from palinreplay.resume.materials import load_resume_materials
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
def recordable(tmp_path, session, llm_config, response, execution_config, monkeypatch):
    monkeypatch.setattr("palinreplay.storage.btrfs.verification.verify_state", lambda *a: None)
    execution_config["interpreter"] = ["bash", "-c"]
    context = deepcopy(session.prefix.context)
    context["config"].setdefault("agent", {})
    context["counters"]["consecutive_format_errors"] = 0
    for key in ("extra_template_vars", "template_vars", "clock", "global_stats", "model_accounting"):
        context.setdefault(key, {})
    context["config"]["model"]["observation_template"] = "{{output.returncode}}:{{output.output}}"
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
    second = response.choices[0].message.tool_calls[0].model_copy(deep=True)
    second.id = "branch-tool-2"
    second.function.arguments = '{"command":"cat /tmp/marker"}'
    response.choices[0].message.tool_calls.append(second)
    complete = Mock(return_value=response)
    monkeypatch.setattr("palinreplay.resume.decision.complete_decision_request", complete)
    decision = decide_resume_start(session, llm_config=llm_config)
    backend = fake.FakeStateBackend()
    monkeypatch.setattr("palinreplay.resume.recording.BtrfsStateBackend", lambda *a, **kw: backend)
    return SimpleNamespace(session=session, decision=decision, backend=backend,
                           complete=complete, parent_next=parent_next)


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
    from palinreplay.core.errors import RecordUsageError

    case = recordable
    node = record_resume_step(case.session, case.decision)
    with pytest.raises(RecordUsageError, match="already consumed"):
        record_resume_step(case.session, case.decision)
    assert case.session.executor.execute.call_count == 2
    assert len(case.backend.captures) == 1
    with RecordStore.open(case.session.output / "record") as store:
        assert store.list(Node) == (node,)
