import pytest

from palinreplay.agents import AgentTask
from palinreplay.core.interfaces import BoundaryAdapter, Payload
from palinreplay.core.models import Node, State
from palinreplay.record.calls import read_call
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
                assert report["execution_status"] == "completed"
                assert report["validation_status"] == "match"
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


@pytest.mark.parametrize("end", ["C1", "C2"])
@pytest.mark.parametrize("different_tool,expected_nodes,expected_calls", [(0, 1, 1), (1, 2, 2)])
def test_early_and_final_tool_differences_keep_correct_boundary(
    tmp_path, end, different_tool, expected_nodes, expected_calls,
):
    source, _, _, _, original = integrated(tmp_path, [response("read", "read"), response("submit")])
    try:
        original.run(AgentTask("task", "Task"))
        materials = load_replay_materials(source, pin_replay(source, "C0", end))
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
                completed = end == "C1" and different_tool == 1
                assert report["execution_status"] == ("completed" if completed else "stopped")
                assert report["validation_status"] == "difference"
                assert report["incomplete_step"] == (different_tool == 0)
                assert ("stop_reason" in report) == (not completed)
                assert report["first_difference"]["source_index"] == 1
                assert report["first_difference"]["source_node_id"] == materials.steps[0].node.node_id
                assert report["first_difference"]["tool_index"] == different_tool
                assert len(store.list(Node)) == expected_nodes
                assert len(sandbox.calls) == expected_calls
                assert store.check_integrity().ok
                if rec.phase == "ready":
                    rec.finish("difference")
    finally:
        source.close()


@pytest.mark.parametrize("end", ["C1", "C2"])
def test_file_difference_distinguishes_completed_range(tmp_path, end):
    source, _, _, _, original = integrated(
        tmp_path, [response("write"), response("read"), response("submit")]
    )
    try:
        original.run(AgentTask("task", "Task"))
        materials = load_replay_materials(source, pin_replay(source, "C0", end))
        context, sandbox = ReplayContext(materials), Sandbox()
        sandbox.execute = lambda req: sandbox.exec(req["action"]["command"])
        with RecordStore.create(tmp_path / "replay") as store:
            with Recorder(store, task_id="task", agent_name="mini", agent_version="v",
                          config=Payload.json({}),
                          boundary=BoundaryAdapter(context.export, Capture(sandbox))) as rec:
                report = replay_range(rec, context, sandbox, materials,
                                      compare_files=lambda *args: {"equal": False,
                                                                   "different_paths": ["file"]})
                assert report["execution_status"] == ("completed" if end == "C1" else "stopped")
                assert report["validation_status"] == "difference"
                assert ("stop_reason" in report) == (end != "C1")
                assert not report["incomplete_step"]
                assert report["first_difference"] == report["steps"][0]["files"]
                assert report["first_difference"]["source_index"] == 1
                assert report["first_difference"]["different_paths"] == ["file"]
                assert len(sandbox.calls) == 1 and len(store.list(Node)) == 2
                rec.finish("difference")
    finally:
        source.close()


def test_empty_range_is_completed_and_matched(tmp_path):
    source, _, _, _, original = integrated(tmp_path, [response("submit")])
    try:
        original.run(AgentTask("task", "Task"))
        materials = load_replay_materials(source, pin_replay(source, "C0", "C0"))
        context, sandbox = ReplayContext(materials), Sandbox()
        with RecordStore.create(tmp_path / "replay") as store:
            with Recorder(store, task_id="task", agent_name="mini", agent_version="v",
                          config=Payload.json({}),
                          boundary=BoundaryAdapter(context.export, Capture(sandbox))) as rec:
                report = replay_range(rec, context, sandbox, materials)
                assert report["execution_status"] == "completed"
                assert report["validation_status"] == "match"
                assert not report["steps"] and len(report["mappings"]) == 1
                assert not sandbox.calls
                rec.finish()
    finally:
        source.close()


@pytest.mark.parametrize("after_difference", [False, True])
def test_execution_fault_preserves_first_observed_difference(tmp_path, monkeypatch, after_difference):
    source, _, _, _, original = integrated(tmp_path, [response("read"), response("submit")])
    try:
        original.run(AgentTask("task", "Task"))
        materials = load_replay_materials(source, pin_replay(source, "C0", "C1"))
        context, sandbox = ReplayContext(materials), Sandbox()

        def fault(*args):
            raise RuntimeError("execution fault")

        sandbox.execute = fault
        if after_difference:
            sandbox.execute = lambda req: ExecResult("read", b"different", 0, 1)
            monkeypatch.setattr(context, "accept_results", fault)
        report = {}
        with RecordStore.create(tmp_path / "replay") as store:
            with pytest.raises(RuntimeError, match="execution fault"):
                with Recorder(store, task_id="task", agent_name="mini", agent_version="v",
                              config=Payload.json({}),
                              boundary=BoundaryAdapter(context.export, Capture(sandbox))) as rec:
                    replay_range(rec, context, sandbox, materials, report=report)
            assert report["execution_status"] == "failed"
            assert report["validation_status"] == ("difference" if after_difference else "incomplete")
            assert ("first_difference" in report) == after_difference
            assert report["incomplete_step"] and len(store.list(Node)) == 1
            if after_difference:
                assert report["first_difference"]["source_index"] == 1
    finally:
        source.close()


@pytest.mark.parametrize("file_difference", [False, True])
@pytest.mark.parametrize("continue_on_difference", [False, True])
@pytest.mark.parametrize("different_tools", [(0,), (1,), (0, 1, 2)])
@pytest.mark.parametrize("kind", ["output", "exit-code"])
def test_continue_tool_differences_retains_actual_results(
    tmp_path, continue_on_difference, different_tools, kind, file_difference,
):
    source, _, _, _, original = integrated(
        tmp_path, [response("read", "read"), response("read"), response("submit")]
    )
    try:
        original.run(AgentTask("task", "Task"))
        materials = load_replay_materials(source, pin_replay(source, "C0", "C2"))
        context, sandbox = ReplayContext(materials), Sandbox()

        def execute(req):
            result = sandbox.exec(req["action"]["command"])
            if len(sandbox.calls) - 1 in different_tools:
                return ExecResult(result.command, b"actual" if kind == "output" else result.output,
                                  1 if kind == "exit-code" else result.exit_code, 1)
            return result

        sandbox.execute = execute
        with RecordStore.create(tmp_path / "replay") as store:
            with Recorder(store, task_id="task", agent_name="mini", agent_version="v",
                          config=Payload.json({}),
                          boundary=BoundaryAdapter(context.export, Capture(sandbox))) as rec:
                comparisons = []

                def compare(index, previous, node):
                    comparisons.append((index, previous.index, node.index))
                    return {"equal": not (file_difference and index == 0),
                            "different_paths": ["file"] if file_difference and index == 0 else []}

                report = replay_range(rec, context, sandbox, materials, compare_files=compare,
                                      continue_on_difference=continue_on_difference)
                assert comparisons == ([(0, 0, 1), (1, 1, 2)] if continue_on_difference else [])
                assert report["execution_status"] == ("completed" if continue_on_difference else "stopped")
                assert report["validation_status"] == report["status"] == "difference"
                first = report["steps"][0]["tools"][different_tools[0]]
                assert report["first_difference"] == first
                assert first["source_index"] == 1 and first["tool_index"] == different_tools[0]
                actual = read_call(store, first["execution_id"])
                assert store.read_bytes(actual.events[1].payload) == (b"actual" if kind == "output" else b"ok\n")
                details = store.read_json(actual.events[1].details)
                assert details["backend"]["decoded"]["returncode"] == (1 if kind == "exit-code" else 0)
                expected_calls = 3 if continue_on_difference else different_tools[0] + 1
                assert len(sandbox.calls) == expected_calls
                expected_nodes = 3 if continue_on_difference else (1 if different_tools[0] == 0 else 2)
                assert len(store.list(Node)) == expected_nodes
                if continue_on_difference:
                    assert not report["incomplete_step"] and "stop_reason" not in report
                    node = store.list(Node)[1]
                    messages = store.read_json(node.context)["messages"]
                    actual_text = "actual" if kind == "output" else "ok\n"
                    assert messages[-2:][different_tools[0]]["extra"]["raw_output"] == actual_text
                    assert len(report["steps"]) == 2
                    assert [s["starting_divergent"] for s in report["steps"]] == [False, True]
                    assert report["steps"][0]["files"]["equal"] is (not file_difference)
                assert store.check_integrity().ok
                if rec.phase == "ready":
                    rec.finish("Replay difference")
    finally:
        source.close()


@pytest.mark.parametrize("continue_on_difference", [False, True])
def test_file_difference_continues_using_actual_workspace(tmp_path, continue_on_difference):
    source, source_sandbox, _, _, original = integrated(
        tmp_path, [response("write"), response("read-file"), response("read"), response("submit")]
    )
    try:
        original.run(AgentTask("task", "Task"))
        materials = load_replay_materials(source, pin_replay(source, "C0", "C3"))
        context, sandbox = ReplayContext(materials), Sandbox()
        reads, comparisons = [], []

        def execute(req):
            command = req["action"]["command"]
            result = sandbox.exec(command)
            if command == "write":
                sandbox.files["file"] = "actual contents"
            if command == "read-file":
                reads.append(sandbox.files["file"])
                return ExecResult(command, reads[-1].encode(), 0, 1)
            return result

        sandbox.execute = execute
        with RecordStore.create(tmp_path / "replay") as store:
            with Recorder(store, task_id="task", agent_name="mini", agent_version="v",
                          config=Payload.json({}),
                          boundary=BoundaryAdapter(context.export, Capture(sandbox))) as rec:
                def compare(index, previous, node):
                    comparisons.append(index)
                    actual = store.read_json(store.get(State, node.state_id).manifest)
                    assert actual["file"] == "actual contents"
                    # Later unchanged endpoints have no new incremental candidates.
                    return {"equal": index != 0, "different_paths": ["file"] if index == 0 else []}

                report = replay_range(rec, context, sandbox, materials, compare_files=compare,
                                      continue_on_difference=continue_on_difference)
                assert report["execution_status"] == ("completed" if continue_on_difference else "stopped")
                assert report["validation_status"] == report["status"] == "difference"
                assert report["first_difference"] == report["steps"][0]["files"]
                assert report["first_difference"]["difference_reason"] == "file-state"
                assert source_sandbox.files["file"] == "changed"
                assert len(store.list(Node)) == (4 if continue_on_difference else 2)
                assert comparisons == ([0, 1, 2] if continue_on_difference else [0])
                assert reads == (["actual contents"] if continue_on_difference else [])
                if continue_on_difference:
                    assert [s["starting_divergent"] for s in report["steps"]] == [False, True, True]
                    assert not report["steps"][1]["tools"][0]["equal"]
                    assert report["steps"][2]["tools"][0]["equal"]
                    assert report["steps"][2]["files"]["equal"]
                    saved = store.read_json(store.list(Node)[2].context)
                    assert saved["messages"][-1]["extra"]["raw_output"] == "actual contents"
                    assert "stop_reason" not in report
                assert store.check_integrity().ok
                rec.finish("Replay difference")
    finally:
        source.close()
