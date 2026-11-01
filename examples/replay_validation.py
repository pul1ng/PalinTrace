"""Real Astropy mini Replay acceptance, with fixed responses and no model service."""

import argparse
import json
import shlex
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from r1_restore_validation import absent, store_hashes

from palinreplay.agents import AgentTask
from palinreplay.agents.mini import MiniSweAgent, load_native
from palinreplay.core.models import Node
from palinreplay.preload.base import open_preload
from palinreplay.record.restore import load_restore_materials, restore_r1_node
from palinreplay.record.state import BtrfsStateBackend
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.replay.session import run_replay
from palinreplay.sandbox.btrfs import BtrfsWorkspace
from palinreplay.sandbox.client import connect_docker, docker_endpoint
from palinreplay.storage.btrfs.models import BtrfsRuntime
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.io import write_json
from palinreplay.storage.rootfs import SCOPE
from palinreplay.storage.store import RecordStore

MARKER = "/opt/palin-replay-controlled"


def python(code):
    return "/opt/miniconda3/bin/python -B -c " + shlex.quote(code)


def commands():
    prefix = f"import os; from pathlib import Path; p=Path({MARKER!r}); "
    stamp = "; [os.utime(f,(1700000000,1700000000)) for f in p.rglob('*') if f.is_file() and not f.is_symlink()]"
    return [
        python(prefix + "p.mkdir(); (p/'edit').write_text('C1'); (p/'delete').write_text('delete'); "
               "(p/'dir').mkdir(); (p/'dir'/'child').write_text('child'); "
               "os.link(p/'edit',p/'alias')" + stamp),
        python(prefix + "(p/'edit').write_text('C2'); (p/'delete').unlink(); "
               "(p/'added').write_text('added'); (p/'dir').rename(p/'moved'); "
               "os.link(p/'alias',p/'third')" + stamp),
        python(prefix + "(p/'third').unlink(); (p/'alias').unlink(); "
               "(p/'alias').write_text('split'); (p/'moved'/'child').write_text('C3')" + stamp),
        "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\ncontrolled\\n'",
    ]


def fixed_response(command, number):
    from litellm import ModelResponse

    return ModelResponse(id=f"fixed-{number}", created=1700000000, model="fixture",
        choices=[{"message": {"role": "assistant", "content": "Fixed controlled decision",
                              "tool_calls": [{"id": f"tool-{number}", "type": "function",
                               "function": {"name": "bash", "arguments": json.dumps({"command": command})}}]},
                  "finish_reason": "tool_calls"}],
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


def record_source(preload, output, client):
    base = open_preload(preload)
    launch = base.metadata.launch.model_copy(update={"network_mode": "none"})
    with BtrfsPool(client, base.metadata.snapshot.location) as pool:
        workspace = BtrfsWorkspace(pool, base.metadata.snapshot, uuid4().hex)
        try:
            workspace.start(launch, base.metadata.bootstrap_image_id,
                            environment=launch.environment, labels={})
            runtime = BtrfsRuntime(source_container_id=workspace.container.id,
                source_image_id=base.metadata.source_image_id,
                bootstrap_image_id=base.metadata.bootstrap_image_id, launch=launch,
                c0_oracle_sha256=base.metadata.oracle_sha256)
            restored = SimpleNamespace(pool=pool, workspace=workspace, container=workspace.container,
                                       close=workspace.close)
            sandbox = RestoredExecutor(restored, runtime)
            sandbox.inspect = lambda: SimpleNamespace(actual_image_id=runtime.bootstrap_image_id,
                workdir=launch.workdir, environment=launch.environment, shell=launch.shell)
            backend = BtrfsStateBackend(sandbox, public_env_keys=launch.environment, runtime=runtime)
            replies = iter(fixed_response(cmd, i) for i, cmd in enumerate(commands(), 1))
            with RecordStore.create(output / "record") as store:
                agent = MiniSweAgent(sandbox=sandbox, store=store, capture_state=backend.capture,
                    model_config={"model_name": "openai/replay-fixture", "cost_tracking": "ignore_errors",
                                  "model_kwargs": {"num_retries": 0}},
                    agent_config={"system_template": "Controlled Replay acceptance",
                                  "instance_template": "{{task}}", "step_limit": 5, "cost_limit": 0},
                    completion=lambda **request: next(replies), retry_attempts=1)
                load_native()
                from minisweagent.models import litellm_model
                with patch.object(litellm_model.litellm.cost_calculator, "completion_cost", return_value=0):
                    agent.run(AgentTask("astropy-controlled-replay", "Execute the fixed decisions."))
                assert len(store.list(Node)) == 5
            container_id = workspace.container.id
        finally:
            workspace.close()
    assert absent(client, container_id)
    return container_id


def main(preload, output):
    output.mkdir(parents=True, exist_ok=False)
    client = connect_docker(600)
    started = time.monotonic()
    try:
        source = output / "source"
        source.mkdir()
        original = record_source(preload, source, client)
        hashes = store_hashes(source / "record")
        report = run_replay(source / "record", "C1", "C3", output=output / "match", client=client)
        write_json(output / "summary.json", {"source_container_removed": absent(client, original),
                                             "replay": report})
        assert report["status"] == "match", report
        assert [m["index"] for m in report["mappings"]] == [0, 1, 2]
        assert report["statistics"]["actual_model_calls"] == 0
        assert report["cleanup_confirmed"]
        comparisons = []
        with RecordStore.open(output / "match" / "record") as store:
            for node in store.list(Node):
                materials = load_restore_materials(store, node.node_id)
                assert absent(client, materials.runtime.source_container_id)
                with restore_r1_node(store, node.node_id, client=client, docker_host=docker_endpoint(),
                                     report_path=output / f"restore-new-C{node.index}.json") as restored:
                    compared = restored.pool.command("verify", source=materials.snapshot.name,
                        workspace=restored.workspace.name, scope=SCOPE,
                        oracle_sha256=materials.runtime.c0_oracle_sha256)
                    comparisons.append(compared)
        full_pairs = []
        with RecordStore.open(source / "record") as origin:
            with RecordStore.open(output / "match" / "record") as current:
                for mapping in report["mappings"][1:]:
                    expected = load_restore_materials(origin, mapping["source_node_id"])
                    actual = load_restore_materials(current, mapping["node_id"])
                    with BtrfsPool(client, actual.snapshot.location) as pool:
                        result = pool.command("verify", source=expected.snapshot.name,
                            workspace=actual.snapshot.name, scope=SCOPE,
                            oracle_sha256=expected.runtime.c0_oracle_sha256)
                        assert result["equal"]
                        full_pairs.append(result)

        # Inject only into this new execution's workspace; source facts and saved
        # commands remain immutable. A matching tool return with a changed file
        # must still stop after preserving the completed real node.
        execute = RestoredExecutor.execute
        injected = []
        def inject(executor, request):
            result = execute(executor, request)
            if not injected:
                injected.append(True)
                executor.exec(python(f"from pathlib import Path; Path({MARKER!r}+'/injected').write_text('different')"),
                              cwd="/", env={"PYTHONDONTWRITEBYTECODE": "1"}, timeout_sec=30)
            return result

        with patch.object(RestoredExecutor, "execute", inject):
            difference = run_replay(source / "record", "C1", "C3", output=output / "difference", client=client)
        assert difference["status"] == "difference" and difference["stop_reason"] == "file-state"
        assert len(difference["steps"]) == 1 and len(difference["mappings"]) == 2
        assert difference["cleanup_confirmed"]
        assert any(path.endswith('/injected') for path in difference["first_difference"]["different_paths"])
        assert store_hashes(source / "record") == hashes
        summary = {"status": "passed", "source_container_removed": True,
                   "source_unchanged": True, "new_trajectory_restores": comparisons,
                   "full_pair_comparisons": full_pairs, "difference": difference,
                   "replay": report, "duration_sec": time.monotonic() - started}
        write_json(output / "summary.json", summary, replace=True)
        print(json.dumps({"status": "passed", "output": str(output),
                          "duration_sec": summary["duration_sec"]}))
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    main(args.preload, args.output)
