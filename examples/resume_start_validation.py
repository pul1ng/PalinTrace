"""Offline Resume-start acceptance for the known 40-step A/B/C recording.

Expected identities were checked independently in the source catalog; this script
never derives its expected node ID from the selector under test.
"""

import argparse
import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from palinreplay.core.errors import RecordUsageError
from palinreplay.core.models import Node, Run, State
from palinreplay.llm import OpenAIChatClient
from palinreplay.resume import select_resume_start
from palinreplay.sandbox.btrfs import BtrfsSandbox, BtrfsWorkspace
from palinreplay.sandbox.docker import DockerSDKSandbox
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.io import write_json
from palinreplay.storage.store import RecordStore

EXPECTED_NODE_ID = "c18d313c6bb04ac98b1ebba48f145ba3"
EXPECTED_RUN_ID = "a9261e45064b44029fa21e2cd2c3635e"
EXPECTED_STATE_ID = "1379ebee3b81426494c547ce5676c1a7"


def forbidden(counts, name):
    def fail(*args, **kwargs):
        counts[name] += 1
        raise AssertionError(f"Resume selection attempted {name}")

    return fail


def main(source: Path, output: Path) -> dict:
    source, output = source.absolute(), output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    counts = {
        "docker_starts": 0, "model_requests": 0,
        "native_initializations": 0, "material_accesses": 0,
    }
    summary = {
        "status": "failed", "source": str(source), "selector": "C20",
        "expected_node_id": EXPECTED_NODE_ID, "counts": counts,
    }
    write_json(output / "acceptance.json", summary)
    try:
        with ExitStack() as guards:
            for owner, method in (
                (DockerSDKSandbox, "start"), (BtrfsSandbox, "start"),
                (BtrfsWorkspace, "start"), (BtrfsPool, "__enter__"),
            ):
                guards.enter_context(patch.object(owner, method, forbidden(counts, "docker_starts")))
            guards.enter_context(patch.object(
                OpenAIChatClient, "complete", forbidden(counts, "model_requests")
            ))
            # The native model class is built by this initializer. Block it
            # without loading mini or creating its temporary configuration.
            guards.enter_context(patch(
                "palinreplay.agents.mini.load_native", forbidden(counts, "native_initializations")
            ))
            for method in ("read_bytes", "read_json", "verify_artifact", "get_node"):
                guards.enter_context(patch.object(
                    RecordStore, method, forbidden(counts, "material_accesses")
                ))

            start = select_resume_start(source, "C20", model="M", max_steps=2)
            summary.update(
                source_store=str(start.source_store), node_id=start.node.node_id,
                run_id=start.node.run_id, state_id=start.node.state_id,
                context=start.node.context.model_dump(mode="json"),
                input_position=start.node.input_position.model_dump(mode="json"),
                model=start.model, max_steps=start.max_steps,
            )
            assert start.node.node_id == EXPECTED_NODE_ID
            assert start.node.index == 20
            assert start.node.run_id == EXPECTED_RUN_ID
            assert start.node.state_id == EXPECTED_STATE_ID
            assert start.model == "M" and start.max_steps == 2

            # Read back using only the two returned source identifiers, with no
            # selector call to generate the expected metadata.
            with RecordStore.open(start.source_store, read_only=True) as reader:
                with reader.snapshot():
                    original = reader.get(Node, start.node.node_id)
                    run = reader.get(Run, original.run_id)
                    state = reader.get(State, original.state_id)
                    comparisons = {
                        "node_id": original.node_id == EXPECTED_NODE_ID,
                        "run_id": start.node.run_id == original.run_id == run.run_id,
                        "state_id": start.node.state_id == original.state_id == state.state_id,
                        "context": start.node.context == original.context,
                        "input_position": start.node.input_position == original.input_position,
                    }
            summary["metadata_comparisons"] = comparisons
            assert all(comparisons.values()), comparisons

            try:
                select_resume_start(source, "C41", model="M", max_steps=2)
            except RecordUsageError as error:
                assert "no committed node" in str(error), str(error)
                summary["missing_node"] = {
                    "selector": "C41", "error_type": type(error).__name__, "error": str(error),
                }
            else:
                raise AssertionError("C41 unexpectedly selected a committed node")
            assert not any(counts.values()), counts
        summary.update(status="passed", conclusion="Resume 第一步『选定续跑起点』验收通过")
    except BaseException as error:
        summary.update(error_type=type(error).__name__, error=str(error))
        raise
    finally:
        write_json(output / "acceptance.json", summary, replace=True)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="recording or record/ directory")
    parser.add_argument("--output", required=True, type=Path, help="new acceptance output directory")
    args = parser.parse_args()
    main(args.source, args.output)
