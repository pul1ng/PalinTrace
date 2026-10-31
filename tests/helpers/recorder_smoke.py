"""Step 4 offline acceptance: a caller-owned fake Agent loop and state backend.

  uv run --locked python examples/recorder_smoke.py --output data/record-step4

All files, model responses and tool effects here are in-memory fixtures.
This exercises Recorder, not Docker, mini-SWE-agent or a real model service.
"""

import argparse
import json
import time
from pathlib import Path

from palinreplay.core.interfaces import (
    AgentSnapshot,
    BoundaryAdapter,
    CallAdapter,
    Payload,
    ResultCapture,
)
from palinreplay.core.models import CaptureInfo, Node, RunEnd, State, ToolGroup
from palinreplay.record.calls import RecordingError, read_call
from palinreplay.record.recorder import Recorder
from palinreplay.storage.store import RecordStore


class FakeAgent:
    def __init__(self):
        self.messages = [{"role": "user", "content": "模拟修改、读取并结束。"}]
        self.input_position = 1

    def receive(self, observation):
        self.messages.append({**observation, "adopted_by": "step4-fixture-agent"})
        self.input_position += 1

    def export(self, boundary):
        return AgentSnapshot(
            context=Payload.json({"messages": self.messages, "agent_version": "fixture-v1"}),
            input_position=Payload.json({"next_message": self.input_position}),
        )


class FakeStateBackend:
    def __init__(self):
        self.files = {
            "/testbed/a": {"hex": b"initial\r\n".hex(), "mode": 0o644},
            "/opt/config": {"hex": b"outside".hex(), "mode": 0o600},
        }
        self.captures = []
        self.tool_calls = 0

    def execute(self, request):
        self.tool_calls += 1
        action = request["action"]
        if action == "timeout":
            raise TimeoutError("fixture tool outcome unknown")
        if action == "write":
            self.files[request["path"]] = {"hex": request["hex"], "mode": 0o640}
        elif action == "error":
            return {"output": "known fixture error", "exit_code": 2}
        return {"output": "fixture tool returned", "exit_code": 0}

    def capture(self, store, boundary, previous):
        self.captures.append(boundary)
        # Inspect the entire actual fixture state at EVERY boundary, even for
        # read-only/no-tool steps; modes and paths participate in equivalence.
        data = store.put_json(self.files)
        manifest = store.put_json({"paths": sorted(self.files), "data": data.sha256})
        runtime = store.put_json({"backend": "in-memory-fixture", "network": "none"})
        capture = CaptureInfo(
            scope="step4-fixture-files",
            included=("/testbed", "/opt"),
            attributes=("content", "mode", "path-existence"),
            consistency="fixture",
            complete=True,
            limitations=(
                "In-memory fixture only; no real filesystem, process or service capture.",
            ),
        )
        if previous and (
            previous.backend == "step4-fixture-v1"
            and previous.capture == capture
            and previous.manifest == manifest
            and previous.runtime == runtime
            and previous.artifacts == (data,)
        ):
            return previous
        return State(
            backend="step4-fixture-v1",
            capture=capture,
            manifest=manifest,
            runtime=runtime,
            artifacts=(data,),
            logical_parent_id=previous.state_id if previous else None,
        )


def model_adapter():
    return CallAdapter(
        name="fixture.model",
        execute=lambda request: {"role": "assistant", "tool_calls": request["tool_calls"]},
        capture_request=Payload.json,
        capture_result=lambda value: ResultCapture(Payload.json(value), "fixture-model-json"),
        visibility="observed-attempt",
    )


def tool_adapter(backend):
    return CallAdapter(
        name="fixture.tool",
        execute=backend.execute,
        capture_request=Payload.json,
        capture_result=lambda value: ResultCapture(
            Payload.json(value),
            "fixture-tool-json",
            outcome="error" if value["exit_code"] else "returned",
        ),
        visibility="observed-attempt",
    )


def adopt(agent, call, observation):
    call.deliver(observation, encode=Payload.json, receive=agent.receive)
    call.accept(agent.messages[-1], encode=Payload.json)


def recorder(store, agent, backend, **kwargs):
    return Recorder(
        store,
        task_id="step4-synthetic",
        agent_name="fixture-agent",
        agent_version="1",
        config=Payload.json({"synthetic": True, "network": "none"}),
        boundary=BoundaryAdapter(agent.export, backend.capture),
        **kwargs,
    )


def answer(rec, agent, tool_ids):
    step = rec.begin_step()
    model = step.model(model_adapter(), {"messages": agent.messages, "tool_calls": tool_ids})
    response = model.invoke()
    adopt(agent, model, response)
    step.declare_tools(model, tuple(response["tool_calls"]))
    return step


def main(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    agent, backend = FakeAgent(), FakeStateBackend()
    expected_contexts, nodes = [], []
    with RecordStore.create(output / "record") as store:
        with recorder(store, agent, backend) as rec:
            nodes.append(rec.current_node)
            expected_contexts.append(json.loads(agent.export(None).context.data))
            for requests in (
                [
                    {"action": "write", "path": "/testbed/a", "hex": b"new\x00\xff".hex()},
                    {"action": "write", "path": "/opt/config", "hex": b"outside changed".hex()},
                ],
                [{"action": "read"}, {"action": "error"}],
                [],  # Explicit empty tool group: a final, accepted answer.
            ):
                ids = tuple(f"tool-{i}" for i in range(len(requests)))
                step = answer(rec, agent, ids)
                for tool_id, request in zip(ids, requests, strict=True):
                    call = step.tool(tool_adapter(backend), request, tool_call_id=tool_id)
                    value = call.invoke()
                    adopt(agent, call, {"role": "tool", "tool_call_id": tool_id, **value})
                nodes.append(rec.commit_step())
                expected_contexts.append(json.loads(agent.export(None).context.data))
            rec.finish()
        run_id = rec.run.run_id
    captures, tool_count = len(backend.captures), backend.tool_calls
    del agent, backend  # Readback must depend only on the persisted record.
    with RecordStore.open(output / "record") as store:
        loaded = [store.get_node(n.node_id) for n in nodes]
        assert [n.index for n in loaded] == [0, 1, 2, 3]
        assert [n.parent_node_id for n in loaded[1:]] == [n.node_id for n in loaded[:-1]]
        assert [store.read_json(n.context) for n in loaded] == expected_contexts
        assert [store.read_json(n.input_position)["next_message"] for n in loaded] == [1, 4, 7, 8]
        assert loaded[0].state_id != loaded[1].state_id
        assert loaded[1].state_id == loaded[2].state_id == loaded[3].state_id
        old = store.get(State, loaded[0].state_id)
        assert store.read_json(old.artifacts[0])["/testbed/a"]["hex"] == b"initial\r\n".hex()
        error = read_call(store, loaded[2].tool_execution_ids[-1])
        assert error.events[1].outcome == "error" and error.phase == "accepted"
        assert store.list(RunEnd, run_id=run_id)[0].status == "completed"
        assert [g.tool_call_ids for g in store.list(ToolGroup)] == [
            ("tool-0", "tool-1"),
            ("tool-0", "tool-1"),
            (),
        ]
        integrity = store.check_integrity()
        assert integrity.ok and not integrity.incomplete_steps

    agent, backend = FakeAgent(), FakeStateBackend()
    with RecordStore.create(output / "unknown") as store:
        with recorder(store, agent, backend) as rec:
            step = answer(rec, agent, ("timeout",))
            call = step.tool(tool_adapter(backend), {"action": "timeout"}, tool_call_id="timeout")
            try:
                call.invoke()
            except TimeoutError:
                pass
            else:
                raise AssertionError("fixture timeout did not propagate")
            try:
                rec.commit_step()
            except RecordingError:
                pass
            else:
                raise AssertionError("unknown result was published as a complete node")
        assert backend.tool_calls == 1 and len(backend.captures) == 1
    with RecordStore.open(output / "unknown") as store:
        assert len(store.list(Node)) == 1
        assert store.list(RunEnd)[0].status == "failed"
        integrity = store.check_integrity()
        assert integrity.ok and integrity.incomplete_steps and integrity.pending_executions
    report = {
        "status": "passed",
        "synthetic": True,
        "nodes": [n.node_id for n in nodes],
        "state_captures": captures,
        "normal_tool_calls": tool_count,
        "multi_step_reopen_verified": True,
        "state_sharing_verified": True,
        "old_context_and_state_unchanged": True,
        "accepted_known_error_committed": True,
        "empty_tool_answer_committed": True,
        "unknown_tail_not_published_or_retried": True,
        "duration_sec": round(time.monotonic() - started, 3),
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(main(parser.parse_args().output), ensure_ascii=False, indent=2))
