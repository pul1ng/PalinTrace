from palinreplay.agents import AgentTask
from palinreplay.core.interfaces import BoundaryAdapter, Payload
from palinreplay.record.recorder import Recorder
from palinreplay.replay.context import ReplayContext
from palinreplay.replay.materials import load_replay_materials
from palinreplay.replay.plan import pin_replay
from palinreplay.replay.step import execute_step
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
