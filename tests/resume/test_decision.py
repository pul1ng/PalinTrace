from copy import deepcopy
from types import SimpleNamespace

import pytest

from palinreplay.resume.context import ResumePrefix
from palinreplay.resume.decision import build_resume_messages


@pytest.fixture
def session():
    messages = [{"role": "system", "content": "Use bash"}, {"role": "user", "content": "Task"}]
    for index in range(1, 21):
        call_id = f"service-tool-{index}"
        messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": call_id, "type": "function", "function": {
                    "name": "bash", "arguments": '{"command":"pwd"}',
                },
            }], "extra": {"actions": [{"tool_call_id": call_id, "command": "pwd"}]}},
            {"role": "tool", "tool_call_id": call_id, "content": f"result {index}"},
        ])
    materials = SimpleNamespace(
        context={"messages": messages, "counters": {"n_calls": 20, "cost": 0}},
        input_position={"next_message": 42},
    )
    return SimpleNamespace(prefix=ResumePrefix(materials))


def test_inherited_messages_are_an_independent_copy(session):
    saved = deepcopy(session.prefix.context)
    messages = build_resume_messages(session)
    assert len(messages) == 42 and messages == saved["messages"]
    assert messages[-1]["role"] == "tool"
    assert messages[-1]["tool_call_id"] == "service-tool-20"
    messages[2]["tool_calls"][0]["function"]["arguments"] = '{"command":"changed"}'
    messages[2]["extra"]["actions"][0]["command"] = "changed"
    messages[-1]["content"] = "changed"
    messages.append({"role": "user", "content": "branch only"})
    assert session.prefix.context == saved
    assert session.prefix.input_position == {"next_message": 42}
