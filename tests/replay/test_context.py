from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from palinreplay.replay.context import ReplayContext
from palinreplay.sandbox.base import ExecResult


def test_uses_real_mini_observation_rendering_and_prefix(tmp_path):
    materials = SimpleNamespace(
        context={"messages": [{"role": "user", "content": "prefix"}], "template_vars": {}},
        input_position={"next_message": 1, "adoptions": [{"execution_id": "source"}]},
        config={"model": {"observation_template": "{{output.returncode}}:{{output.output}}"}},
    )
    context = ReplayContext(materials)
    history = SimpleNamespace(tools=(SimpleNamespace(request={"action": {"tool_call_id": "a"}}),
                                     SimpleNamespace(request={"action": {"tool_call_id": "b"}})))
    outputs = [ExecResult("a", b"a\xff\r\nb\rc", 0, 1), ExecResult("b", b"failed", 3, 2)]
    messages = context.observations(history, outputs)
    assert [m["tool_call_id"] for m in messages] == ["a", "b"]
    assert messages[0]["content"] == "0:a�\nb\nc"
    assert messages[1]["extra"]["returncode"] == 3
    assert context.messages == materials.context["messages"]
    with pytest.raises(ValueError, match="incomplete"):
        context.observations(history, outputs[:1])
    assert context.accept_results(SimpleNamespace(tools=()), [], []) == []
    assert context.export(None).input_position.data == b'{"adoptions":[],"next_message":1}\n'


@pytest.mark.parametrize("ids", [("a",), ("a", "b", "c"), ("b", "a"), ("a", None)])
def test_malformed_observations_fail_before_any_replay_admission(monkeypatch, ids):
    materials = SimpleNamespace(
        context={"messages": [{"role": "user", "content": "prefix"}]},
        input_position={}, config={},
    )
    context = ReplayContext(materials)
    messages = [{"role": "tool", "tool_call_id": identity} for identity in ids]
    calls = [SimpleNamespace(operation=SimpleNamespace(tool_call_id=i)) for i in ("a", "b")]
    monkeypatch.setattr(context, "observations", lambda *args: messages)
    context.adopt = Mock()
    with pytest.raises(ValueError, match="mini tool observations do not align with recorded calls"):
        context.accept_results(SimpleNamespace(tools=(1, 2)), calls, [1, 2])
    context.adopt.assert_not_called()
    assert context.messages == materials.context["messages"]
    assert context.adoptions == []
