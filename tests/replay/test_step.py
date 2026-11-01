import pytest

from palinreplay.agents import AgentTask
from palinreplay.core.interfaces import BoundaryAdapter, Payload
from palinreplay.core.models import Node
from palinreplay.record.recorder import Recorder
from palinreplay.replay.context import ReplayContext
from palinreplay.replay.materials import load_replay_materials
from palinreplay.replay.plan import pin_replay
from palinreplay.replay.run import replay_range
from palinreplay.replay.step import execute_step
from palinreplay.sandbox.base import ExecResult
from palinreplay.storage.store import RecordStore
from tests.agents.test_mini_agent import (  # noqa: F401
    Capture,
    Sandbox,
    deterministic,
    integrated,
    response,
)


def test_real_execution_is_recorded_with_new_calls_and_context(tmp_path):
    source, _, _, _, original = integrated(tmp_path, [response("write"), response("submit")])
    try:
        original.run(AgentTask("task", "Task"))
        materials = load_replay_materials(source, pin_replay(source, "C0", "C1"))
        context, sandbox = ReplayContext(materials), Sandbox()
        sandbox.execute = lambda req: sandbox.exec(req["action"]["command"], cwd=req["cwd"],
                                                 env=req["env"], timeout_sec=req["timeout_sec"])
        with RecordStore.create(tmp_path / "replay") as store:
            with Recorder(store, task_id="task", agent_name="mini-swe-agent", agent_version="fixture",
                          config=Payload.json({}),
                          boundary=BoundaryAdapter(context.export, Capture(sandbox))) as rec:
                node, results = execute_step(rec, context, sandbox, materials.steps[0])
                assert node.index == 1 and sandbox.files["file"] == "changed"
                assert sandbox.calls[0][0] == "write" and results[0].output == b"ok\n"
                assert node.model_execution_id != materials.plan.end.model_execution_id
                assert node.tool_execution_ids != materials.plan.end.tool_execution_ids
                assert store.read_json(node.context)["messages"][-1]["extra"]["raw_output"] == "ok\n"
                assert store.read_json(node.input_position)["adoptions"][-1]["execution_id"] == node.tool_execution_ids[0]
                assert store.check_integrity().ok
                rec.finish()
    finally:
        source.close()


def test_fixed_c20_to_c23_range_becomes_c0_to_c3(tmp_path):
    responses = [response("write")] * 24 + [response("submit")]
    source, _, _, _, original = integrated(tmp_path, responses, agent_config={"step_limit": 30})
    try:
        original.run(AgentTask("task", "Task"))
        materials = load_replay_materials(source, pin_replay(source, "C20", "C23"))
        context, sandbox = ReplayContext(materials), Sandbox()
        sandbox.files["file"] = "changed"
        sandbox.execute = lambda req: sandbox.exec(req["action"]["command"])
        with RecordStore.create(tmp_path / "replay") as store:
            with Recorder(store, task_id="task", agent_name="mini", agent_version="fixture",
                          config=Payload.json({}),
                          boundary=BoundaryAdapter(context.export, Capture(sandbox))) as rec:
                report = replay_range(rec, context, sandbox, materials,
                                      compare_files=lambda *args: {"equal": True})
                assert report["status"] == "match"
                assert [m["source_index"] for m in report["mappings"]] == [20, 21, 22, 23]
                assert [m["index"] for m in report["mappings"]] == [0, 1, 2, 3]
                assert len(sandbox.calls) == 3
                saved = store.read_json(rec.current_node.context)
                assert saved["counters"]["n_calls"] == 23
                assert saved["replay"]["historical_answer_supplies"] == 3
                assert saved["replay"]["actual_model_calls"] == 0
                rec.finish()
    finally:
        source.close()


@pytest.mark.parametrize("different_tool,expected_nodes,expected_calls", [(0, 1, 1), (1, 2, 2)])
def test_early_and_final_tool_differences_keep_correct_boundary(tmp_path, different_tool, expected_nodes, expected_calls):
    source, _, _, _, original = integrated(tmp_path, [response("read", "read"), response("submit")])
    try:
        original.run(AgentTask("task", "Task"))
        materials = load_replay_materials(source, pin_replay(source, "C0", "C2"))
        context, sandbox = ReplayContext(materials), Sandbox()
        def execute(req):
            result = sandbox.exec(req["action"]["command"])
            if len(sandbox.calls) - 1 == different_tool:
                return ExecResult(result.command, b"different", 0, 1)
            return result
        sandbox.execute = execute
        with RecordStore.create(tmp_path / "replay") as store:
            with Recorder(store, task_id="task", agent_name="mini", agent_version="v", config=Payload.json({}), boundary=BoundaryAdapter(context.export, Capture(sandbox))) as rec:
                report = replay_range(rec, context, sandbox, materials, compare_files=lambda *args: {"equal": True})
                assert report["status"] == "difference"
                assert len(store.list(Node)) == expected_nodes
                assert len(sandbox.calls) == expected_calls
                assert store.check_integrity().ok
                if rec.phase == "ready":
                    rec.finish("difference")
    finally:
        source.close()
