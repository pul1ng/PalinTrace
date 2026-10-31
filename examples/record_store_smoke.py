"""Offline step-2 acceptance: two steps, reopen, and isolated corruption cases.

Run from the project root with an unused output directory:
  uv run --locked python examples/record_store_smoke.py --output data/record-step2

Only synthetic data is used. No Agent, model, Docker or historical command runs.
"""

import argparse
import json
import shutil
from pathlib import Path

from palinreplay.core.models import (
    Attempt,
    CaptureInfo,
    Event,
    Execution,
    Node,
    Operation,
    Run,
    State,
    Step,
)
from palinreplay.storage.store import IntegrityError, RecordStore


def save_call(store, step, kind, raw, adopted):
    operation = store.save(
        Operation(run_id=step.run_id, step_id=step.step_id, kind=kind, name=f"fake.{kind}")
    )
    execution = store.save(
        Execution(
            run_id=step.run_id,
            operation_id=operation.operation_id,
            index=1,
            request=store.put_json({"step": step.index, "kind": kind}),
        )
    )
    store.append_event(execution.execution_id, "dispatched")
    store.append_event(
        execution.execution_id, "result", payload=store.put_bytes(raw), outcome="returned"
    )
    observation = store.put_json(adopted)
    store.append_event(execution.execution_id, "delivered", payload=observation)
    store.append_event(execution.execution_id, "accepted", payload=observation)
    return execution


def save_state(store, data):
    content = store.put_bytes(data)
    return store.save(
        State(
            backend="fixture-files-v1",
            capture=CaptureInfo(
                scope="synthetic-file",
                included=("/testbed/example.txt",),
                attributes=("content",),
                consistency="fixture",
                complete=True,
                limitations=("synthetic file material; no real environment capture",),
            ),
            manifest=store.put_json({"/testbed/example.txt": content.model_dump()}),
            runtime=store.put_json({"backend": "fixture", "network": "none"}),
            artifacts=(content,),
        )
    )


def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    clean = output / "record"
    with RecordStore.create(clean) as store:
        run = store.save(
            Run(
                task_id="synthetic-two-step",
                agent_name="fixture",
                agent_version="1",
                config=store.put_json({"synthetic": True}),
            )
        )
        attempt = store.save(Attempt(run_id=run.run_id))
        messages = [{"role": "user", "content": "合成的两步记录样例"}]
        state = save_state(store, b"before\r\n")
        node = store.commit_node(
            Node(
                run_id=run.run_id,
                attempt_id=attempt.attempt_id,
                index=0,
                context=store.put_json(messages),
                input_position=store.put_json({"message_count": len(messages)}),
                state_id=state.state_id,
                accepted_event_sequence=0,
            )
        )
        nodes = [node]
        for index in (1, 2):
            step = store.save(
                Step(
                    run_id=run.run_id,
                    attempt_id=attempt.attempt_id,
                    index=index,
                    parent_node_id=node.node_id,
                )
            )
            answer = {"role": "assistant", "content": f"synthetic step {index}"}
            observation = {"role": "tool", "content": f"adopted result {index}"}
            model = save_call(store, step, "model", b"provider response\r\n", answer)
            tool = save_call(store, step, "tool", b"raw output\x00\xff\r\n", observation)
            messages.extend((answer, observation))
            if index == 1:
                state = save_state(store, b"after\r\n")
            node = store.commit_node(
                Node(
                    run_id=run.run_id,
                    attempt_id=attempt.attempt_id,
                    index=index,
                    parent_node_id=node.node_id,
                    step_id=step.step_id,
                    context=store.put_json(messages),
                    input_position=store.put_json({"message_count": len(messages)}),
                    state_id=state.state_id,
                    accepted_event_sequence=store.list(Event)[-1].sequence,
                    model_execution_id=model.execution_id,
                    tool_operation_ids=(tool.operation_id,),
                    tool_execution_ids=(tool.execution_id,),
                )
            )
            nodes.append(node)
    with RecordStore.open(clean) as store:
        assert all(store.get_node(item.node_id) == item for item in nodes)
        assert store.read_json(node.context) == messages
        assert nodes[1].state_id == nodes[2].state_id
        assert store.check_integrity().ok
    report = {
        "status": "passed",
        "synthetic": True,
        "nodes": len(nodes),
        "steps": 2,
        "shared_state_preserves_distinct_nodes": True,
        "reopen_verified": True,
        "faults": {},
    }
    for fault in ("missing", "corrupt"):
        damaged = output / fault
        shutil.copytree(clean, damaged)
        ref = state.artifacts[0]
        target = damaged / "artifacts" / ref.sha256[:2] / ref.sha256[2:]
        if fault == "missing":
            target.unlink()
        else:
            target.chmod(0o600)
            target.write_bytes(b"X" * ref.size)
        with RecordStore.open(damaged) as store:
            try:
                store.get_node(node.node_id)
            except IntegrityError as exc:
                report["faults"][fault] = {"detected": True, "message": str(exc)}
            else:
                raise AssertionError(f"{fault} material was accepted")
            assert not store.check_integrity().ok
    with RecordStore.open(clean) as store:
        assert store.check_integrity().ok
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.output), ensure_ascii=False, indent=2))
