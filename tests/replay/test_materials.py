from dataclasses import replace

import pytest

from palinreplay.agents import AgentTask
from palinreplay.core.models import Event
from palinreplay.llm import LLMConfig
from palinreplay.replay.materials import _tool_request, load_replay_materials
from palinreplay.replay.plan import pin_replay
from palinreplay.storage.store import IntegrityError
from tests.agents.test_mini_agent import deterministic, integrated, response  # noqa: F401


def test_reads_accepted_materials_and_effective_execution_settings(tmp_path, monkeypatch):
    store, sandbox, _, _, agent = integrated(
        tmp_path, [response("bytes", "bad"), response("submit")],
        environment_config={"env": {"CUSTOM": "saved"}, "timeout": 17},
    )
    try:
        agent.run(AgentTask("task", "Task"))
        def no_credentials(*args, **kwargs):
            raise AssertionError("Replay read model credentials")

        monkeypatch.setattr(LLMConfig, "load", no_credentials)
        materials = load_replay_materials(store, pin_replay(store, "C0", "C1"))
        step = materials.steps[0]
        assert step.tool_ids == ("tool-0", "tool-1")
        assert step.model.messages[0]["extra"]["actions"][0]["command"] == "bytes"
        first = step.tools[0]
        assert first.raw == b"a\xff\r\nb\rc"
        assert first.request == {
            "action": {"command": "bytes", "tool_call_id": "tool-0"},
            "cwd": "/testbed", "env": {"PAGER": "cat", "CUSTOM": "saved"},
            "interpreter": ["bash", "-c"], "timeout_sec": 17,
        }
        assert step.tools[1].details["backend"]["decoded"]["returncode"] == 1
        assert "messages" not in step.model.request
        assert first.document()["raw_base64"] == "Yf8NCmINYw=="
        assert len(materials.context["messages"]) == 2
        assert len(sandbox.calls) == 3
        for change in ({"timeout_sec": 0}, {"interpreter": []}, {"action": {"command": "other"}}):
            with pytest.raises(IntegrityError):
                _tool_request(replace(first, request=first.request | change), first.request["action"])
        plan = materials.plan
        unsupported = replace(plan, run=plan.run.model_copy(update={"agent_version": "other"}))
        with pytest.raises(IntegrityError, match="version"):
            load_replay_materials(store, unsupported)
    finally:
        store.close()


def test_missing_used_material_fails_without_tool_execution(tmp_path):
    store, sandbox, _, _, agent = integrated(tmp_path, [response("bytes"), response("submit")])
    try:
        agent.run(AgentTask("task", "Task"))
        plan = pin_replay(store, "C0", "C1")
        raw = next(e.payload for e in store.list(Event)
                   if e.execution_id == plan.end.tool_execution_ids[0] and e.kind == "result")
        path = store.root / "artifacts" / raw.sha256[:2] / raw.sha256[2:]
        path.unlink()
        count = len(sandbox.calls)
        with pytest.raises(IntegrityError):
            load_replay_materials(store, plan)
        assert len(sandbox.calls) == count
    finally:
        store.close()
