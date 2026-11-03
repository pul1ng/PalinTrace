from contextlib import contextmanager

import pytest

from palinreplay.agents import AgentTask
from palinreplay.core.interfaces import BoundaryAdapter, Payload
from palinreplay.core.models import Node, State
from palinreplay.record.calls import read_call
from palinreplay.record.recorder import Recorder
from palinreplay.replay.config import replay_config
from palinreplay.replay.context import ReplayContext
from palinreplay.replay.materials import load_replay_materials
from palinreplay.replay.plan import pin_replay
from palinreplay.replay.run import replay_range
from palinreplay.sandbox.base import ExecResult
from palinreplay.storage.store import RecordStore
from tests.agents.test_mini_agent import (  # noqa: F401
    Capture,
    Sandbox,
    deterministic,
    integrated,
    response,
)


@contextmanager
def recording(store, materials, context, sandbox):
    rec = Recorder(store, task_id="task", agent_name="mini", agent_version="fixture",
                   config=Payload.json(replay_config(materials)),
                   boundary=BoundaryAdapter(context.export, Capture(sandbox)))
    rec.start_from_source()
    try:
        yield rec
    except BaseException as error:
        rec.__exit__(type(error), error, error.__traceback__)
        raise
    else:
        if rec.phase == "ready":
            if rec.current_node is not None:
                rec.finish()
            else:
                rec.cancel()  # Successful zero-node termination is enabled in task 7.
    finally:
        rec.close()


def executor(sandbox):
    sandbox.execute = lambda req: sandbox.exec(req["action"]["command"], cwd=req["cwd"],
                                             env=req["env"], timeout_sec=req["timeout_sec"])
    return sandbox


def test_fixed_c20_range_records_only_actual_source_numbered_nodes(tmp_path):
    source, _, _, _, original = integrated(
        tmp_path, [response("write")] * 24 + [response("submit")], agent_config={"step_limit": 30},
    )
    try:
        original.run(AgentTask("task", "Task"))
        materials = load_replay_materials(source, pin_replay(source, "C20", "C23"))
        context, sandbox = ReplayContext(materials), executor(Sandbox())
        with RecordStore.create(tmp_path / "replay") as store:
            with recording(store, materials, context, sandbox) as rec:
                report = replay_range(rec, context, sandbox, materials)
                assert report["execution_status"] == "completed"
                assert [m["source_index"] for m in report["mappings"]] == [21, 22, 23]
                assert [m["index"] for m in report["mappings"]] == [21, 22, 23]
                assert [n.index for n in store.list(Node)] == [21, 22, 23]
                assert len(sandbox.calls) == 3
                node = rec.current_node
                assert node.model_execution_id != materials.plan.end.model_execution_id
                assert node.tool_execution_ids != materials.plan.end.tool_execution_ids
                saved = store.read_json(node.context)
                assert saved["counters"]["n_calls"] == 23
                assert saved["replay"]["historical_answer_supplies"] == 3
                assert saved["replay"]["actual_model_calls"] == 0
                assert saved["messages"][-1]["extra"]["raw_output"] == "ok\n"
                assert store.check_integrity().ok
    finally:
        source.close()


@pytest.mark.parametrize("end", ["C1", "C2"])
@pytest.mark.parametrize("different_tool", [0, 1])
def test_exit_mismatch_saves_actual_boundary_and_stops_even_at_endpoint(tmp_path, end, different_tool):
    source, _, _, _, original = integrated(tmp_path, [response("read", "read"), response("submit")])
    try:
        original.run(AgentTask("task", "Task"))
        materials = load_replay_materials(source, pin_replay(source, "C0", end))
        context, sandbox = ReplayContext(materials), Sandbox()
        requests = []
        def execute(req):
            requests.append(req)
            result = sandbox.exec(req["action"]["command"])
            return ExecResult(result.command, b"actual", 1 if len(requests) - 1 == different_tool else 0, 1)
        sandbox.execute = execute
        with RecordStore.create(tmp_path / "replay") as store:
            with recording(store, materials, context, sandbox) as rec:
                report = replay_range(rec, context, sandbox, materials)
                assert report["execution_status"] == "stopped"
                assert report["stop_reason"] == "exit-code"
                assert report["incomplete_step"] is (different_tool == 0)
                mismatch = report["first_difference"]
                assert mismatch["source_index"] == 1 and mismatch["tool_index"] == different_tool
                assert mismatch["source_exit_code"] == 0 and mismatch["actual_exit_code"] == 1
                actual = read_call(store, mismatch["execution_id"])
                assert store.read_bytes(actual.events[1].payload) == b"actual"
                assert requests == [t.request for t in materials.steps[0].tools[:different_tool + 1]]
                assert len(store.list(Node)) == different_tool
                assert len(report["mappings"]) == different_tool
                if different_tool:
                    assert store.list(Node)[0].index == 1
                    assert store.read_json(rec.current_node.context)["messages"][-1]["extra"]["raw_output"] == "actual"
                assert store.check_integrity().ok
    finally:
        source.close()


def test_matching_exit_codes_keep_different_outputs_and_files_and_continue(tmp_path, monkeypatch):
    source, source_sandbox, _, _, original = integrated(
        tmp_path, [response("write"), response("read-file"), response("read"), response("submit")],
    )
    try:
        original.run(AgentTask("task", "Task"))
        materials = load_replay_materials(source, pin_replay(source, "C0", "C3"))
        context, sandbox = ReplayContext(materials), Sandbox()
        def execute(req):
            result = sandbox.exec(req["action"]["command"])
            sandbox.files["file"] = "actual contents"
            return ExecResult(result.command, b"different\xff\r\n", 0, 1)
        sandbox.execute = execute
        def no_comparison(*args, **kwargs):
            pytest.fail("runtime output/file comparison was called")
        monkeypatch.setattr("palinreplay.replay.compare.compare_tool", no_comparison)
        with RecordStore.create(tmp_path / "replay") as store:
            with recording(store, materials, context, sandbox) as rec:
                report = replay_range(rec, context, sandbox, materials)
                assert report["execution_status"] == "completed"
                assert len(sandbox.calls) == len(store.list(Node)) == 3
                assert "first_difference" not in report
                assert source_sandbox.files["file"] == "changed"
                assert sandbox.files["file"] == "actual contents"
                assert store.read_json(store.get(State, rec.current_node.state_id).manifest)["file"] == "actual contents"
                call = read_call(store, rec.current_node.tool_execution_ids[0])
                assert store.read_bytes(call.events[1].payload) == b"different\xff\r\n"
                assert all("files" not in step for step in report["steps"])
                assert store.check_integrity().ok
    finally:
        source.close()


@pytest.mark.parametrize("after_mismatch", [False, True])
def test_execution_or_adoption_fault_preserves_observed_mismatch(tmp_path, monkeypatch, after_mismatch):
    source, _, _, _, original = integrated(tmp_path, [response("read"), response("submit")])
    try:
        original.run(AgentTask("task", "Task"))
        materials = load_replay_materials(source, pin_replay(source, "C0", "C1"))
        context, sandbox = ReplayContext(materials), Sandbox()
        def fault(*args):
            raise RuntimeError("execution fault")
        sandbox.execute = fault
        if after_mismatch:
            sandbox.execute = lambda req: ExecResult("read", b"actual", 1, 1)
            monkeypatch.setattr(context, "accept_results", fault)
        report = {}
        with RecordStore.create(tmp_path / "replay") as store:
            with pytest.raises(RuntimeError, match="execution fault"):
                with recording(store, materials, context, sandbox) as rec:
                    replay_range(rec, context, sandbox, materials, report=report)
            assert report["execution_status"] == "failed"
            assert ("first_difference" in report) is after_mismatch
            assert report["incomplete_step"] and not store.list(Node)
            assert store.check_integrity().ok
    finally:
        source.close()


def test_equal_nonzero_exits_allow_historical_recovery(tmp_path):
    source, _, _, _, original = integrated(tmp_path, [response("bad"), response("read"), response("submit")])
    try:
        original.run(AgentTask("task", "Task"))
        materials = load_replay_materials(source, pin_replay(source, "C0", "C2"))
        context, sandbox = ReplayContext(materials), executor(Sandbox())
        with RecordStore.create(tmp_path / "replay") as store:
            with recording(store, materials, context, sandbox) as rec:
                report = replay_range(rec, context, sandbox, materials)
                assert report["execution_status"] == "completed" and len(sandbox.calls) == 2
                tool = report["steps"][0]["tools"][0]
                assert tool["source_exit_code"] == tool["actual_exit_code"] == 1
                assert store.check_integrity().ok
    finally:
        source.close()
