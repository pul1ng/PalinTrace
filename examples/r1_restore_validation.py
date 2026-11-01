"""Opt-in real Docker/Btrfs R1 acceptance; no model service or history replay.

Run from the repository with the docker extra. An existing preload is reused;
all mutations happen in a new writable workspace, never in its read-only base.
"""

import argparse
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from palinreplay.core.interfaces import (
    AgentSnapshot,
    BoundaryAdapter,
    CallAdapter,
    Payload,
    ResultCapture,
)
from palinreplay.core.models import Node
from palinreplay.preload.base import open_preload
from palinreplay.record.recorder import Recorder
from palinreplay.record.restore import load_restore_materials, restore_r1_node
from palinreplay.record.state import BtrfsStateBackend
from palinreplay.sandbox.btrfs import BtrfsWorkspace
from palinreplay.sandbox.client import connect_docker, docker_endpoint
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.io import write_json
from palinreplay.storage.rootfs import SCOPE
from palinreplay.storage.store import IntegrityError, RecordStore

MARKER = "/opt/palin-r1-controlled"
FIRST = f"""import os
from pathlib import Path
p = Path({MARKER!r})
assert not p.exists()
p.mkdir()
(p/'modified').write_text('C1')
(p/'deleted').write_text('C1 only')
(p/'type-change').write_text('file at C1')
(p/'attributes').write_text('attributes')
os.chmod(p/'attributes', 0o640)
os.chown(p/'attributes', 123, 456)
os.utime(p/'attributes', (1700000001, 1700000001))
os.link(p/'attributes', p/'hardlink')
(p/'symlink').symlink_to('attributes')
(p/'directory').mkdir()
"""
SECOND = f"""import os
from pathlib import Path
p = Path({MARKER!r})
(p/'modified').write_text('C2')
(p/'deleted').unlink()
(p/'added').write_text('C2 only')
(p/'type-change').unlink()
(p/'type-change').mkdir()
(p/'type-change'/'child').write_text('directory at C2')
(p/'directory').rename(p/'renamed')
(p/'symlink').unlink()
(p/'symlink').symlink_to('hardlink')
os.chmod(p/'attributes', 0o600)
os.chown(p/'attributes', 321, 654)
os.utime(p/'attributes', (1700000002, 1700000002))
"""


def python(container, code):
    result = container.exec_run(
        ["/opt/miniconda3/bin/python", "-B", "-c", code],
        environment={"PYTHONDONTWRITEBYTECODE": "1"}, workdir="/",
    )
    if result.exit_code:
        raise AssertionError(result.output.decode(errors="replace"))
    return {"exit_code": result.exit_code, "output": result.output.decode(errors="replace")}


def store_hashes(path):
    return {
        str(item.relative_to(path)): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(path.rglob("*"))
        if item.is_file() and not item.name.endswith(("-wal", "-shm", "-journal"))
    }


def absent(client, name):
    try:
        client.containers.get(name)
    except Exception as error:
        if getattr(error, "status_code", None) == 404:
            return True
        raise
    return False


def record_controlled(preload, output, client):
    base = open_preload(preload)
    launch = base.metadata.launch.model_copy(update={"network_mode": "none"})
    messages = [{"role": "user", "content": "R1 controlled acceptance"}]
    owner = uuid4().hex
    with BtrfsPool(client, base.metadata.snapshot.location) as pool:
        workspace = BtrfsWorkspace(pool, base.metadata.snapshot, owner)
        try:
            workspace.start(
                launch, base.metadata.bootstrap_image_id, environment=launch.environment, labels={}
            )
            source_container = workspace.container.id
            sandbox = SimpleNamespace(
                pool=pool, _container=workspace.container, workspace=workspace.name,
                preload=SimpleNamespace(base=base), _network="none", check_root=lambda: None,
            )
            backend = BtrfsStateBackend(sandbox, public_env_keys=tuple(launch.environment))

            def export(boundary):
                return AgentSnapshot(
                    Payload.json({
                        "messages": messages, "config": {"mode": "controlled"},
                        "counters": {"steps": boundary.index, "llm_calls": 0},
                    }),
                    Payload.json({"next_message": len(messages), "step": boundary.index}),
                )

            def adopt(call, value):
                call.deliver(value, encode=Payload.json, receive=messages.append)
                call.accept(messages[-1], encode=Payload.json)

            with RecordStore.create(output / "record") as store:
                write_json(store.root / "btrfs.json", pool.location.model_dump(mode="json"))
                with Recorder(
                    store, task_id="r1-controlled", agent_name="deterministic-fixture",
                    agent_version="1", config=Payload.json({"llm_calls": 0}),
                    boundary=BoundaryAdapter(export, backend.capture),
                ) as recorder:
                    for index, code in enumerate((FIRST, SECOND), 1):
                        step = recorder.begin_step()
                        # Persist a fixed decision through normal Recorder ports; no LLM is called.
                        decision = step.model(CallAdapter(
                            name="fixed-decision",
                            execute=lambda request: {"role": "assistant", "tool": request["tool"]},
                            capture_request=Payload.json,
                            capture_result=lambda value: ResultCapture(Payload.json(value), "fixture"),
                        ), {"tool": f"change-{index}"})
                        adopt(decision, decision.invoke())
                        step.declare_tools(decision, (f"change-{index}",))
                        tool = step.tool(CallAdapter(
                            name="python", execute=lambda request: python(workspace.container, request),
                            capture_request=Payload.json,
                            capture_result=lambda value: ResultCapture(Payload.json(value), "exec"),
                        ), code, tool_call_id=f"change-{index}")
                        adopt(tool, tool.invoke())
                        recorder.commit_step()
                    recorder.finish("controlled trajectory complete")
        finally:
            workspace.close()
    assert absent(client, source_container)
    return source_container


def check_files(container, index):
    python(container, f"""import os, stat
from pathlib import Path
p = Path({MARKER!r})
i = {index}
if i == 0:
    assert not p.exists()
else:
    assert (p/'modified').read_text() == f'C{{i}}'
    assert (p/'deleted').exists() == (i == 1)
    assert (p/'added').exists() == (i == 2)
    assert (p/'type-change').is_dir() == (i == 2)
    assert (p/('directory' if i == 1 else 'renamed')).is_dir()
    s = (p/'attributes').stat()
    assert (s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode), int(s.st_mtime)) == (
        (123, 456, 0o640, 1700000001) if i == 1 else (321, 654, 0o600, 1700000002))
    assert s.st_ino == (p/'hardlink').stat().st_ino
    assert os.readlink(p/'symlink') == ('attributes' if i == 1 else 'hardlink')
""")


def validate_nodes(source, output, client, *, controlled=False):
    before = store_hashes(source)
    results = []
    with RecordStore.open(source) as store:
        nodes = store.list(Node)
        selected = (nodes[0], nodes[len(nodes) // 2], nodes[-1])
        for node in selected:
            materials = load_restore_materials(store, node.node_id)
            assert absent(client, materials.runtime.source_container_id)
            for trial in range(2):
                report = output / f"C{node.index}-{trial}.json"
                operations = []
                original_command = BtrfsPool.command

                def command(pool, op, **values):
                    operations.append(op)
                    return original_command(pool, op, **values)

                with patch.object(BtrfsPool, "command", command):
                    with restore_r1_node(
                        store, node.node_id, client=client, docker_host=docker_endpoint(),
                        report_path=report,
                    ) as restored:
                        assert "verify" not in operations and "import" not in operations
                        assert restored.context == store.read_json(node.context)
                        assert restored.input_position == store.read_json(node.input_position)
                        assert restored.config == store.read_json(materials.run.config)
                        assert restored.report["status"] == "ready"
                        if controlled:
                            check_files(restored.container, node.index)
                        compared = restored.pool.command(
                            "verify", source=materials.snapshot.name,
                            workspace=restored.workspace.name, scope=SCOPE,
                            oracle_sha256=materials.runtime.c0_oracle_sha256,
                        )
                        assert compared["equal"]
                        difference_rejected = False
                        if trial == 0:
                            python(restored.container, "from pathlib import Path; "
                                   "Path('/opt/r1-local-only').write_text('local mutation')")
                            try:
                                restored.pool.command(
                                    "verify", source=materials.snapshot.name,
                                    workspace=restored.workspace.name, scope=SCOPE,
                                    oracle_sha256=materials.runtime.c0_oracle_sha256,
                                )
                            except IntegrityError as error:
                                assert "differs" in str(error)
                                difference_rejected = True
                            assert difference_rejected
                        else:
                            python(restored.container,
                                   "from pathlib import Path; assert not Path('/opt/r1-local-only').exists()")
                        resource_names = {
                            "container": restored.workspace.container_name,
                            "volume": restored.workspace.volume_name,
                            "helper": restored.pool.name,
                            "pool_volume": restored.pool.location.volume,
                        }
                    saved = json.loads(report.read_text())
                    assert saved["cleanup_confirmed"]
                    assert absent(client, resource_names["container"])
                    assert absent(client, resource_names["helper"])
                    try:
                        client.volumes.get(resource_names["volume"])
                    except Exception as error:
                        assert getattr(error, "status_code", None) == 404
                    else:
                        raise AssertionError("temporary root volume remains")
                    client.volumes.get(resource_names["pool_volume"])
                results.append({
                    "node_index": node.index, "node_id": node.node_id, "trial": trial,
                    "comparison": compared, "difference_rejected": difference_rejected,
                    "context_matches": True, "input_position_matches": True,
                    "cleanup_confirmed": True, "operations": operations,
                    "report": report.name,
                })
    assert store_hashes(source) == before
    write_json(output / "source-manifest.json", before)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    client = connect_docker(600)
    try:
        source_container = record_controlled(args.preload, args.output, client)
        results = validate_nodes(args.output / "record", args.output, client, controlled=True)
        write_json(args.output / "summary.json", {
            "status": "passed", "scenario": "controlled", "llm_calls": 0,
            "source_container_removed": source_container, "restores": results,
            "source_unchanged": True, "duration_seconds": time.monotonic() - started,
            "daemon_id": client.info()["ID"],
        })
    finally:
        client.close()


if __name__ == "__main__":
    main()
