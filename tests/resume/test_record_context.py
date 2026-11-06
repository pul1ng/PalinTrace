from copy import deepcopy
from unittest.mock import Mock

import pytest

from palinreplay.core.interfaces import BoundaryAdapter, Payload
from palinreplay.core.models import Node, Operation, ToolGroup
from palinreplay.record.calls import read_call
from palinreplay.record.recorder import Recorder
from palinreplay.resume.record import _claim_decision
from palinreplay.resume.record_context import ResumeRecordContext
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake
from tests.resume import test_record

session = test_record.session
llm_config = test_record.llm_config
response = test_record.response
decision = test_record.decision


def context_recorder(store, context):
    return Recorder(store, task_id="fixture", agent_name="fixture", agent_version="1",
                    config=Payload.json({}),
                    boundary=BoundaryAdapter(context.export, fake.FakeStateBackend().capture))


@pytest.mark.parametrize("decision,guided", [(None, False), ("branch guide", True)],
                         indirect=["decision"])
def test_saved_response_is_adopted_and_seals_original_tools(
    tmp_path, session, decision, monkeypatch, guided,
):
    original = deepcopy(session.prefix.context)
    no_request = Mock(side_effect=AssertionError("unexpected model request"))
    monkeypatch.setattr("palinreplay.resume.decision.complete_decision_request", no_request)
    with _claim_decision(session, decision) as saved:
        context = ResumeRecordContext(session, saved)
        with RecordStore.create(tmp_path / "record") as store:
            rec = context_recorder(store, context)
            rec.start()
            step = rec.begin_step()
            call = context.accept_decision(step, saved)
            trace = read_call(store, call.execution_id)
            assert [event.kind for event in trace.events] == [
                "dispatched", "result", "delivered", "accepted",
            ]
            assert store.read_json(trace.execution.request) == saved["request"]
            assert store.read_json(trace.events[1].payload) == saved["raw_response"]
            details = store.read_json(trace.events[1].details)["backend"]
            assert details["accounting"] == decision.accounting
            assert details["actual_model_calls"] == 0
            group, = store.list(ToolGroup)
            assert group.model_execution_id == call.execution_id
            assert group.tool_call_ids == ("branch-tool-1", "branch-tool-2")
            assert len(context.messages) == 43 + guided
            assert context.messages[:42] == original["messages"]
            assert context.messages[-1]["tool_calls"] == decision.assistant_message["tool_calls"]
            assert context.messages[-1]["extra"]["actions"] == decision.actions
            assert len(store.list(Operation)) == 1
            assert len(store.list(Node)) == 1  # only the ordinary fixture's C0
            assert context.adoptions[0]["start"] == 42 + guided
            assert context.adoptions[0]["stop"] == 43 + guided
            assert context.context["counters"]["n_calls"] == 21
            assert context.context["config"]["model"]["model_name"] == "openai/M"
            assert context.context["resume"]["first_decision_accounting"] == decision.accounting
            assert context.context["resume"]["remaining_steps"] == 0
            rec.cancel()
            rec.close()
    assert session.prefix.context == original
    assert no_request.call_count == 0 and not session.executor.mock_calls


def test_inherited_adoptions_are_provenance_and_local_events_start_fresh(session, decision):
    session.materials.input_position["adoptions"] = [{
        "start": 40, "stop": 42, "execution_id": "parent-tool", "event_sequence": 160,
    }]
    with _claim_decision(session, decision) as saved:
        context = ResumeRecordContext(session, saved)
        assert context.adoptions == []
        assert context.inherited_position == session.input_position
        assert context.context["resume"]["inherited_input_position"] == session.input_position
        assert context.context["model_accounting"]["cost_known"] is False
