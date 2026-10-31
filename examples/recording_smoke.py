"""Step 3 offline acceptance: fake model, two tools, Agent adoption and unknown outcome.

  uv run --locked python examples/recording_smoke.py --output data/record-step3

This explicitly builds fixture C0/C1 for verification, not a Recorder loop.
No model API, shell command, Docker container or network operation is executed.
"""

import argparse
import json
from pathlib import Path

from palinreplay.core.interfaces import CallAdapter, Payload, ResultCapture
from palinreplay.core.models import Attempt, CaptureInfo, Event, Node, Run, State, Step
from palinreplay.record.calls import RecordingPorts, read_call
from palinreplay.sandbox.base import ExecResult
from palinreplay.storage.store import IncompleteRecordError, RecordStore


class FakeAgent:
    def __init__(self):
        self.messages = [{"role": "user", "content": "运行两个模拟工具并保存结果。"}]

    def receive(self, message):
        self.messages.append({**message, "accepted_by": "fixture-agent"})
        return self.messages[-1]


def main(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    counts = {"model": 0, "tool": 0}
    agent = FakeAgent()

    def model_backend(request):
        counts["model"] += 1
        return {
            "id": "fixture-response-1",
            "model": request["model"],
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "tool-1",
                                "function": {"name": "shell", "arguments": "emit sample"},
                            },
                            {
                                "id": "tool-2",
                                "function": {"name": "shell", "arguments": "emit error"},
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 9, "total_tokens": 21},
            "provider_extension": {"preserved": True},
        }

    def tool_backend(request):
        counts["tool"] += 1
        if request["command"] == "timeout fixture":
            raise TimeoutError("synthetic deadline; operation outcome unknown")
        code = 2 if request["command"] == "emit error" else 0
        raw = b"synthetic error\r\n" if code else b"synthetic bytes\x00\xff\r\n"
        return ExecResult(request["command"], raw, code, 0.001)

    model_adapter = CallAdapter(
        name="fixture.model",
        execute=model_backend,
        capture_request=Payload.json,
        capture_result=lambda value: ResultCapture(
            raw=Payload.json(value),
            source="model-api-json",
            details={"usage": value["usage"]},
            limitations=("Fixture API value; no HTTP wire bytes or hidden reasoning.",),
        ),
        visibility="observed-attempt",
    )
    tool_adapter = CallAdapter(
        name="fixture.shell",
        execute=tool_backend,
        capture_request=Payload.json,
        capture_context=lambda request: Payload.json({"cwd": request["cwd"], "backend": "fake"}),
        capture_result=lambda value: ResultCapture(
            raw=Payload(value.output),
            source="merged-tool-output",
            details={
                "exit_code": value.exit_code,
                "duration_sec": value.duration_sec,
                "streams": "stdout/stderr merged",
            },
        ),
        visibility="observed-attempt",
    )
    with RecordStore.create(output / "record") as store:
        run = store.save(
            Run(
                task_id="step3-synthetic",
                agent_name="fake-agent",
                agent_version="1",
                config=store.put_json({"synthetic": True, "network": "none"}),
            )
        )
        attempt = store.save(Attempt(run_id=run.run_id))
        state = store.save(
            State(
                backend="step3-fixture",
                capture=CaptureInfo(
                    scope="fixture",
                    included=("/fixture",),
                    attributes=("content",),
                    consistency="fixture",
                    complete=True,
                    limitations=("No real environment was captured.",),
                ),
                manifest=store.put_json({"paths": []}),
                runtime=store.put_json({"backend": "fake"}),
                artifacts=(store.put_bytes(b"no filesystem side effects in this fixture"),),
            )
        )
        c0 = store.commit_node(
            Node(
                run_id=run.run_id,
                attempt_id=attempt.attempt_id,
                index=0,
                context=store.put_json(agent.messages),
                input_position=store.put_json({"messages": 1}),
                state_id=state.state_id,
                accepted_event_sequence=0,
            )
        )
        step = store.save(
            Step(
                run_id=run.run_id, attempt_id=attempt.attempt_id, index=1, parent_node_id=c0.node_id
            )
        )
        ports = RecordingPorts(store, step.step_id)
        model = ports.model(
            model_adapter,
            {"messages": agent.messages, "model": "fixture-model-v1", "temperature": 0, "seed": 1},
        )
        response = model.invoke()
        answer = response["choices"][0]["message"]
        model.deliver(answer, encode=Payload.json, receive=agent.receive)
        model.accept(agent.messages[-1], encode=Payload.json)
        tools = []
        expected_outputs = {}
        for definition in answer["tool_calls"]:
            tool = ports.tool(
                tool_adapter,
                {
                    "command": definition["function"]["arguments"],
                    "cwd": "/testbed",
                    "env": {"LANG": "C"},
                    "timeout_sec": 5,
                },
                tool_call_id=definition["id"],
                caused_by_execution_id=model.execution_id,
            )
            result = tool.invoke()
            observation = {
                "role": "tool",
                "tool_call_id": definition["id"],
                "content": result.text[:12],
                "exit_code": result.exit_code,
            }
            tool.deliver(observation, encode=Payload.json, receive=agent.receive)
            tool.accept(agent.messages[-1], encode=Payload.json)
            tools.append(tool)
            expected_outputs[tool.execution_id] = result.output
        c1 = store.commit_node(
            Node(
                run_id=run.run_id,
                attempt_id=attempt.attempt_id,
                index=1,
                step_id=step.step_id,
                parent_node_id=c0.node_id,
                context=store.put_json(agent.messages),
                input_position=store.put_json({"messages": len(agent.messages)}),
                state_id=state.state_id,
                accepted_event_sequence=store.list(Event)[-1].sequence,
                model_execution_id=model.execution_id,
                tool_operation_ids=tuple(tool.operation.operation_id for tool in tools),
                tool_execution_ids=tuple(tool.execution_id for tool in tools),
            )
        )
        unfinished = store.save(
            Step(
                run_id=run.run_id, attempt_id=attempt.attempt_id, index=2, parent_node_id=c1.node_id
            )
        )
        failure_ports = RecordingPorts(store, unfinished.step_id)
        timeout = failure_ports.tool(
            tool_adapter,
            {"command": "timeout fixture", "cwd": "/testbed"},
            tool_call_id="timeout-1",
        )
        try:
            timeout.invoke()
        except TimeoutError:
            pass
        else:
            raise AssertionError("timeout did not propagate")
        try:
            failure_ports.retry(timeout)
        except IncompleteRecordError:
            pass
        else:
            raise AssertionError("unknown outcome was retried")

    with RecordStore.open(output / "record") as store:
        traces = [read_call(store, call.execution_id) for call in (model, *tools, timeout)]
        assert [trace.phase for trace in traces] == ["accepted", "accepted", "accepted", "unknown"]
        assert store.read_json(traces[0].events[1].payload) == response
        for trace in traces[1:3]:
            assert (
                store.read_bytes(trace.events[1].payload)
                == expected_outputs[trace.execution.execution_id]
            )
            assert store.read_json(trace.events[-1].payload)["accepted_by"] == "fixture-agent"
        audit = store.check_integrity()
        assert audit.ok and audit.incomplete_steps == (unfinished.step_id,)
        assert audit.pending_executions == (timeout.execution_id,)
        assert store.get_node(c1.node_id) == c1
        assert counts == {"model": 1, "tool": 3}
        report = {
            "status": "passed",
            "synthetic": True,
            "backend_calls": counts,
            "reopen_verified": True,
            "raw_tool_bytes_verified": True,
            "complete_model_response_verified": True,
            "agent_adoption_verified": True,
            "unknown_outcome_not_retried": True,
            "incomplete_step_detected": True,
            "calls": [
                {
                    "operation_id": trace.operation.operation_id,
                    "execution_id": trace.execution.execution_id,
                    "kind": trace.operation.kind,
                    "phase": trace.phase,
                    "events": [
                        {"kind": e.kind, "sequence": e.sequence, "outcome": e.outcome}
                        for e in trace.events
                    ],
                }
                for trace in traces
            ],
        }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = main(args.output)
    print(f"{result['status']}: {args.output / 'report.json'}")
