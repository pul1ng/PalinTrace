from copy import deepcopy
from unittest.mock import Mock

import pytest

from palinreplay.agents.mini import (
    complete_decision_request,
    load_native,
    parse_decision_actions,
)
from palinreplay.resume import decide_resume_start
from palinreplay.resume.decision import prepare_branch_decision
from palinreplay.resume.record import _claim_decision, _execute_and_save_group
from palinreplay.resume.record_context import ResumeRecordContext
from palinreplay.sandbox.base import ExecResult
from palinreplay.storage.store import RecordStore
from tests.resume import test_record_context, test_record_tools

session = test_record_tools.session
llm_config = test_record_tools.llm_config
response = test_record_tools.response
execution_config = test_record_tools.execution_config


def api_messages(messages):
    fields = ("role", "content", "tool_calls", "tool_call_id", "function_call")
    return [{key: deepcopy(message[key]) for key in fields if key in message} for message in messages]


@pytest.mark.parametrize("instruction", [None, "unique branch instruction"])
def test_four_requests_retain_the_complete_actual_conversation(
    tmp_path, session, llm_config, response, execution_config, monkeypatch, instruction,
):
    native = load_native()
    session.start.max_steps = 4
    session.prefix.config["model"]["observation_template"] = "{{output.output}}"
    source = deepcopy(session.context)
    replies = []
    for number in range(1, 5):
        reply = response.model_copy(deep=True)
        reply.id = f"response-{number}"
        reply.choices[0].message.content = f"branch decision {number}"
        reply.choices[0].message.tool_calls[0].id = f"branch-tool-{number}"
        replies.append(reply)
    requests = []
    expected = deepcopy(source["messages"])
    if instruction is not None:
        expected.append({"role": "user", "content": instruction})

    def complete(**request):
        public = {k: deepcopy(v) for k, v in request.items() if k not in ("api_key", "api_base")}
        assert public["messages"] == api_messages(expected)
        requests.append(public)
        return replies[len(requests) - 1]

    monkeypatch.setattr(native.litellm, "completion", Mock(side_effect=complete))
    session.executor.execute.side_effect = [
        ExecResult("tool", f"unique actual result {number}\n".encode(), 0, .1)
        for number in range(1, 5)
    ]
    decision = decide_resume_start(session, instruction=instruction, llm_config=llm_config)
    with _claim_decision(session, decision) as saved:
        context = ResumeRecordContext(session, saved)
        with RecordStore.create(tmp_path / "record") as store:
            recorder = test_record_context.context_recorder(store, context)
            recorder.start()
            for number in range(1, 5):
                step = recorder.begin_step()
                if number == 1:
                    evidence = saved
                else:
                    prepared = prepare_branch_decision(session, context.messages, llm_config=llm_config)
                    reply = complete_decision_request(prepared.request, prepared.credentials)
                    raw = reply.model_dump(mode="json")
                    evidence = {**saved, "messages": prepared.messages, "request": prepared.request,
                                "raw_response": raw, "assistant_message": raw["choices"][0]["message"],
                                "actions": parse_decision_actions(prepared.native_model, reply)}
                    context.account_decision(evidence["accounting"])
                context.accept_decision(step, evidence)
                node, _ = _execute_and_save_group(session, recorder, step, context, evidence["actions"])
                expected.extend([
                    evidence["assistant_message"],
                    {"role": "tool", "tool_call_id": f"branch-tool-{number}",
                     "content": f"unique actual result {number}\n"},
                ])
                assert api_messages(store.read_json(node.context)["messages"]) == api_messages(expected)
            recorder.pause("four controlled requests")
            recorder.close()
            assert store.check_integrity().ok
    assert [len(request["messages"]) for request in requests] == [
        42 + int(instruction is not None) + 2 * index for index in range(4)
    ]
    fourth = requests[-1]["messages"]
    assert fourth[1]["content"] == "Task"
    assert any(m.get("content") == "unique actual result 1\n" for m in fourth)
    assert all(sum(m.get("content") == instruction for m in request["messages"]
                   if m["role"] == "user") == int(instruction is not None)
               for request in requests)
    assert len(requests) == session.executor.execute.call_count == 4
    assert session.context == source
