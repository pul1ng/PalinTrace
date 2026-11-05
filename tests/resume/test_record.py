import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from unittest.mock import Mock

import pytest

from palinreplay.core.errors import RecordUsageError
from palinreplay.resume import decide_resume_start
from palinreplay.resume.record import _claim_decision
from tests.resume import test_decision

session = test_decision.session
llm_config = test_decision.llm_config
response = test_decision.response


@pytest.fixture
def decision(session, llm_config, response, monkeypatch, request):
    second = response.choices[0].message.tool_calls[0].model_copy(deep=True)
    second.id = "branch-tool-2"
    second.function.arguments = '{"command":"cat /tmp/marker","label":"keep"}'
    response.choices[0].message.tool_calls.append(second)
    session.start.max_steps = 1
    monkeypatch.setattr("palinreplay.resume.decision.complete_decision_request",
                        Mock(return_value=response))
    return decide_resume_start(session, llm_config=llm_config,
                               instruction=getattr(request, "param", None))


def test_claim_uses_saved_evidence_with_no_remaining_budget(session, decision):
    assert decision.accounting["remaining_steps"] == 0
    with _claim_decision(session, decision) as saved:
        persisted = json.loads(session.report_path.read_text())
        assert persisted["recording"]["status"] == "consuming"
        assert saved["actions"] == decision.actions
        saved["actions"][0]["command"] = "changed copy"
        assert session._decision_evidence["actions"] == decision.actions
    assert not session.executor.mock_calls
    with pytest.raises(RecordUsageError, match="already consumed"):
        with _claim_decision(session, decision):
            pytest.fail("decision consumed twice")


@pytest.mark.parametrize("change", ["command", "id", "order", "messages", "assistant", "raw", "usage"])
def test_changed_decision_is_rejected_without_consuming(session, decision, change):
    changed = deepcopy(decision)
    if change in ("command", "id"):
        changed.actions[0]["command" if change == "command" else "tool_call_id"] = "changed"
    elif change == "order":
        changed.actions.reverse()
    elif change == "messages":
        changed.messages[0]["content"] = "changed"
    elif change == "assistant":
        changed.assistant_message["content"] = "changed"
    elif change == "raw":
        changed.raw_response["id"] = "changed"
    else:
        changed.accounting["usage"] = {}
    before = deepcopy(session._decision_evidence)
    with pytest.raises(RecordUsageError, match="differs"):
        with _claim_decision(session, changed):
            pytest.fail("modified decision accepted")
    assert not session._record_attempt and not session.executor.mock_calls
    assert session._decision_evidence == before
    with _claim_decision(session, decision):
        pass


@pytest.mark.parametrize("closed", ["phase", "executor"])
def test_closed_session_is_rejected_before_consumption(session, decision, closed):
    if closed == "phase":
        session.report["phase"] = "complete"
    else:
        session.executor.state = "closed"
    with pytest.raises(RecordUsageError, match="active session"):
        with _claim_decision(session, decision):
            pytest.fail("closed session accepted")
    assert not session._record_attempt and not session.executor.mock_calls


def test_claim_requires_a_saved_success(session, response):
    with pytest.raises(RecordUsageError, match="successful saved decision"):
        with _claim_decision(session, None):
            pytest.fail("missing decision accepted")
    assert not session.executor.mock_calls


def test_consumption_survives_failure_and_cannot_be_replayed(session, decision, monkeypatch):
    monkeypatch.setattr("palinreplay.resume.record.write_json", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        with _claim_decision(session, decision):
            pytest.fail("unpersisted claim delivered")
    with pytest.raises(RecordUsageError, match="already consumed"):
        with _claim_decision(session, decision):
            pytest.fail("failed consumption retried")
    assert session._record_attempt["status"] == "failed"
    assert session.report["decision"]["status"] == "succeeded"
    assert not session.executor.mock_calls


def test_concurrent_consumption_is_rejected(session, decision):
    entered, release = Event(), Event()
    def consume():
        with _claim_decision(session, decision):
            entered.set()
            assert release.wait(10)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(consume)
        try:
            assert entered.wait(10)
            with pytest.raises(RecordUsageError, match="already being consumed"):
                with _claim_decision(session, decision):
                    pytest.fail("concurrent consumption accepted")
        finally:
            release.set()
        future.result()
    assert not session.executor.mock_calls
