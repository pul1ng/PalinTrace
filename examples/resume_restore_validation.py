"""Opt-in Resume restoration acceptance for the pinned 40-step A/B/C recording.

Only explicit inspection commands run. Source damage is confined to ordinary
copied stores; task mutations are confined to disposable writable snapshots.
"""

import argparse
import hashlib
import json
import shlex
import shutil
import sqlite3
import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from palinreplay.core.errors import IntegrityError, RecordUsageError
from palinreplay.core.models import Node, Run, RunEnd
from palinreplay.core.serialization import json_bytes
from palinreplay.llm import OpenAIChatClient
from palinreplay.record.recorder import Recorder
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.resume import restore_resume_start, select_resume_start
from palinreplay.resume.materials import load_resume_materials
from palinreplay.resume.validation import validate_r1
from palinreplay.sandbox.btrfs import BtrfsWorkspace
from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.btrfs.pool import OWNER_LABEL, BtrfsPool
from palinreplay.storage.io import outside_store, write_json
from palinreplay.storage.rootfs import SCOPE
from palinreplay.storage.store import RecordStore

EXPECTED_NODE_ID = "c18d313c6bb04ac98b1ebba48f145ba3"
EXPECTED_RUN_ID = "a9261e45064b44029fa21e2cd2c3635e"
EXPECTED_STATE_ID = "1379ebee3b81426494c547ce5676c1a7"
MANAGED_FILE = "/testbed/astropy/modeling/separable.py"
_INSPECTION = ContextVar("resume_acceptance_inspection", default=None)


def forbidden(counts, name):
    def fail(*args, **kwargs):
        counts[name] += 1
        raise AssertionError(f"Resume restoration attempted {name}")
    return fail


@contextmanager
def no_new_work(counts):
    with ExitStack() as stack:
        original_exec = RestoredExecutor.exec

        def guarded_exec(executor, command, **kwargs):
            if _INSPECTION.get() != (id(executor), command):
                forbidden(counts, "historical_tool_reexecutions")()
            return original_exec(executor, command, **kwargs)

        stack.enter_context(patch.object(RestoredExecutor, "exec", guarded_exec))
        for owner, method, counter in (
            (OpenAIChatClient, "complete", "model_requests"),
            (RestoredExecutor, "execute", "historical_tool_reexecutions"),
            (RecordStore, "commit_node", "branch_node_commits"),
            (Recorder, "commit_step", "branch_node_commits"),
            (Recorder, "start", "branch_node_commits"),
            (Recorder, "start_from_source", "branch_node_commits"),
        ):
            stack.enter_context(patch.object(owner, method, forbidden(counts, counter)))
        for target, counter in (
            ("palinreplay.agents.mini.load_native", "native_initializations"),
            ("palinreplay.agents.mini.MiniSweAgent.__init__", "native_initializations"),
            ("palinreplay.replay.step.execute_step", "historical_tool_reexecutions"),
            ("palinreplay.replay.run.execute_step", "historical_tool_reexecutions"),
            ("palinreplay.restoration.services.prepare_services", "service_preparations"),
            ("palinreplay.replay.session.prepare_services", "service_preparations"),
            ("palinreplay.restoration.services.start_service", "service_preparations"),
            ("openai.resources.chat.completions.Completions.create", "model_requests"),
            ("openai.resources.chat.completions.AsyncCompletions.create", "model_requests"),
        ):
            stack.enter_context(patch(target, forbidden(counts, counter)))
        yield


def file_inventory(path):
    # Include every file, including SQLite sidecars if any exist.
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob("*")) if p.is_file()}


def node_inventory(store_path):
    with RecordStore.open(store_path, read_only=True) as store:
        return [node.model_dump(mode="json") for node in store.list(Node)]


def assert_absent(collection, name):
    try:
        collection.get(name)
    except Exception as error:
        if getattr(error, "status_code", None) != 404:
            raise
    else:
        raise AssertionError(f"temporary resource remains: {name}")


def pool_audit(client, snapshot):
    with BtrfsPool(client, snapshot.location) as pool:
        source = pool.command("info", name=snapshot.name)
        assert source["readonly"] and source["uuid"] == str(snapshot.uuid)
        result = pool.container.exec_run([
            "python3", "-c",
            "import json; from pathlib import Path; "
            "print(json.dumps(sorted(p.name for p in Path('/pool/workspaces').iterdir())))",
        ])
        assert result.exit_code == 0
        workspaces = json.loads(result.output)
        helper, device = pool.name, pool.device
    assert_absent(client.containers, helper)
    # Independently read sysfs after close, and check the persistent pool's
    # temporary mount record. This observer mounts the Docker volume read-only.
    observer = "pr-resume-audit-" + uuid4().hex
    code = (
        "import json,sys; from pathlib import Path; "
        "p=Path('/sys/block')/sys.argv[1]/'loop/backing_file'; "
        "print(json.dumps({'loop_released':not (p.exists() and p.read_text().strip()),"
        "'mount_record_absent':not Path('/storage/mount.json').exists()}))"
    )
    detached = json.loads(client.containers.run(
        snapshot.location.helper_image_id, ["python3", "-c", code, Path(device).name],
        name=observer, labels={OWNER_LABEL: observer}, network_mode="none", remove=True,
        volumes={snapshot.location.volume: {"bind": "/storage", "mode": "ro"}},
        use_config_proxy=False,
    ))
    assert all(detached.values()), detached
    assert_absent(client.containers, observer)
    client.volumes.get(snapshot.location.volume)
    return {"source": source, "workspaces": workspaces, "helper_removed": True, **detached}


def check(executor, actions, label, command):
    token = _INSPECTION.set((id(executor), command))
    try:
        result = executor.exec(command)
    finally:
        _INSPECTION.reset(token)
    actions.append({"label": label, "container_id": executor._container.id,
                    "command": command, "exit_code": result.exit_code, "output": result.text})
    assert result.exit_code == 0, actions[-1]
    return result.text


def compare_prefix(session, *, model="M"):
    materials, node = session.materials, session.start.node
    index, message_count = node.index, 2 * node.index + 2
    with RecordStore.open(session.start.source_store, read_only=True) as store:
        context = store.read_json(node.context)
        assert session.context == context
        assert session.input_position == store.read_json(node.input_position)
        assert materials.config == store.read_json(materials.run.config)
        assert materials.context_payload.data == store.read_bytes(node.context)
        assert materials.input_position_payload.data == store.read_bytes(node.input_position)
        assert materials.config_payload.data == store.read_bytes(materials.run.config)
        next_id, = store.find_node_ids(index=index + 1, run_id=node.run_id)
        next_context = store.read_json(store.get(Node, next_id).context)
        ends = store.list(RunEnd, run_id=node.run_id)
        assert ends, "fixture parent Run must be finished"
    messages = session.context["messages"]
    assert len(messages) == message_count and session.input_position["next_message"] == message_count
    assert session.prefix.counters["n_calls"] == index
    assert messages[-1]["tool_call_id"] == f"service-tool-{index}"
    assert len(next_context["messages"]) == message_count + 2
    assert next_context["messages"][:message_count] == messages
    assert not any(m.get("tool_call_id") == f"service-tool-{index + 1}" for m in messages)
    assert session.start.model == model and session.start.max_steps == 2
    return {"message_count": message_count, "next_message": message_count, "historical_calls": index,
            "messages": [{"index": i, "equal": a == b,
                          "sha256": hashlib.sha256(json_bytes(a)).hexdigest()}
                         for i, (a, b) in enumerate(zip(messages, context["messages"], strict=True))],
            "raw_payloads_equal": True, "configuration_equal": True,
            f"C{index + 1}_excluded": True, "parent_run_ended": True}


def check_environment(session, actions):
    executor, materials = session.executor, session.materials
    expected = materials.runtime.launch.environment | materials.config["environment"]["env"]
    assert executor._env == expected
    assert executor._shell == ("bash", "-c")
    assert executor._timeout == materials.config["environment"]["timeout"]
    assert check(executor, actions, "pwd", "pwd").strip() == "/testbed"
    code = "import os,json; print(json.dumps(dict(os.environ)))"
    result = session.restored.container.exec_run(
        ["/opt/miniconda3/bin/python", "-B", "-c", code], environment=executor._env,
    )
    assert result.exit_code == 0
    before_shell = json.loads(result.output)
    assert all(before_shell.get(k) == v for k, v in expected.items())
    actions.append({"label": "saved_environment_before_shell", "exit_code": result.exit_code,
                    "container_id": executor._container.id, "environment": expected})
    after_shell = json.loads(check(executor, actions, "environment_after_bash",
                                  "python -B -c " + shlex.quote(code)))
    assert all(after_shell.get(k) == v for k, v in expected.items() if k != "PATH")
    assert after_shell["PATH"].endswith(expected["PATH"])
    launch = materials.runtime.launch
    session.restored.container.reload()
    attrs = session.restored.container.attrs
    assert attrs["Image"] == materials.runtime.bootstrap_image_id
    assert attrs["Config"]["WorkingDir"] == launch.workdir
    assert attrs["Config"]["User"] == launch.user
    assert attrs["HostConfig"]["NetworkMode"] == launch.network_mode
    assert attrs["HostConfig"]["Memory"] == launch.memory_bytes
    assert attrs["HostConfig"]["NanoCpus"] == launch.nano_cpus
    return {"cwd": executor._workdir, "interpreter": executor._shell, "timeout": executor._timeout,
            "environment": expected, "bash_PATH": after_shell["PATH"],
            "launch_configuration_equal": True,
            "note": "Saved BASH_ENV activates conda in .bashrc and prefixes PATH."}


def checked_report(client, output):
    report = json.loads((output / "report.json").read_text())
    assert report["cleanup_confirmed"] and not report["cleanup_errors"]
    resources = report.get("resources")
    if resources:
        for field in ("container_name", "pool_container_name"):
            assert_absent(client.containers, resources[field])
        assert_absent(client.volumes, resources["volume_name"])
        assert resources["cleanup_confirmed"]
    return report


def corrupt_copy(start, output, fault):
    copy = output / "material-copies" / fault / "record"
    shutil.copytree(start.source_store, copy)
    target = copy / "artifacts" / start.node.context.sha256[:2] / start.node.context.sha256[2:]
    if fault == "missing":
        target.unlink()
    elif fault == "digest":
        target.unlink()  # Copied immutable artifacts retain their read-only mode.
        target.write_bytes(b"x" * start.node.context.size)
    elif fault == "json":
        with RecordStore.open(copy, read_only=False) as store:
            run = store.get(Run, start.node.run_id)
            ref = store.put_bytes(b"invalid JSON", media_type="application/json")
        body = json_bytes(run.model_copy(update={"config": ref}).model_dump(mode="json"))
        with sqlite3.connect(copy / "catalog.sqlite") as catalog:
            catalog.execute("UPDATE records SET body=?, sha256=? WHERE kind='run' AND id=?",
                            (body.decode(), hashlib.sha256(body).hexdigest(), run.run_id))
    else:
        raise ValueError(fault)
    return select_resume_start(copy, "C20", model="M", max_steps=2, run_id=EXPECTED_RUN_ID)


def main(source, output):
    source, output = Path(source).resolve(), Path(output).absolute()
    recording = source.parent if source.name == "record" else source
    outside_store(recording, output)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    counts = dict.fromkeys(("model_requests", "historical_tool_reexecutions", "service_preparations",
                           "branch_node_commits", "native_initializations"), 0)
    summary = {"status": "failed", "source": str(recording), "counts": counts,
               "inspection_actions": [], "restores": [], "rejections": {}, "r1_calls": []}
    write_json(output / "acceptance.json", summary)
    client = None
    started = time.monotonic()
    before = file_inventory(recording)
    write_json(output / "source-files-before.json", before)
    original_start = BtrfsWorkspace.start
    workspace_starts = []
    operations = []
    original_command = BtrfsPool.command

    def record_start(workspace, *args, **kwargs):
        workspace_starts.append(workspace.name)
        return original_start(workspace, *args, **kwargs)

    def command(pool, op, **values):
        evidence = {"op": op, "pool": pool.name, "arguments": values}
        operations.append(evidence)
        try:
            evidence["result"] = original_command(pool, op, **values)
            return evidence["result"]
        except BaseException as error:
            evidence["error"] = str(error)
            raise

    def r1(materials, executor):
        entry = {"container_id": executor._container.id, "workspace": executor.workspace}
        summary["r1_calls"].append(entry)
        try:
            entry["result"] = validate_r1(materials, executor)
            return entry["result"]
        except BaseException as error:
            entry["result"] = error.r1_report
            raise

    try:
        with no_new_work(counts), patch.object(BtrfsWorkspace, "start", record_start), \
                patch.object(BtrfsPool, "command", command), \
                patch("palinreplay.resume.validation.validate_r1", r1):
            start = select_resume_start(source, "C20", model="M", max_steps=2)
            assert (start.node.node_id, start.node.run_id, start.node.state_id) == (
                EXPECTED_NODE_ID, EXPECTED_RUN_ID, EXPECTED_STATE_ID)
            materials = load_resume_materials(start)
            nodes_before = node_inventory(start.source_store)
            assert len(nodes_before) == 41
            write_json(output / "source-nodes-before.json", nodes_before)
            client = connect_docker(600)
            assert not client.containers.list(all=True, filters={"label": OWNER_LABEL})
            summary["pool_before"] = pool_audit(client, materials.snapshot)
            marker = "/testbed/.palin-resume-" + uuid4().hex
            actions = summary["inspection_actions"]
            for trial in ("normal", "caller-error"):
                print(f"Restoring C20: {trial}", flush=True)
                destination = output / trial
                original = RuntimeError("controlled Resume caller error")
                try:
                    # First session owns its client; the second borrows this audit client.
                    with restore_resume_start(start, destination,
                                              None if trial == "normal" else client) as session:
                        prefix = compare_prefix(session)
                        environment = check_environment(session, actions)
                        assert session.report["r1"]["status"] == "passed"
                        assert session.report["r1"]["scope"] == SCOPE
                        check(session.executor, actions, "marker_absent", "test ! -e " + marker)
                        if trial == "normal":
                            check(session.executor, actions, "marker_write_read",
                                  f"printf resume-marker > {marker} && cat {marker}")
                        else:
                            raise original
                except RuntimeError as error:
                    assert trial == "caller-error" and error is original
                report = checked_report(client, destination)
                assert report["restore_status"] == "passed"
                assert report["caller"]["status"] == ("completed" if trial == "normal" else "failed")
                assert report["cleanup"]["client"] == (
                    "closed" if trial == "normal" else "caller_managed")
                assert session.executor.state == "closed"
                summary["restores"].append({"trial": trial, "prefix": prefix,
                                            "environment": environment, "report": str(destination),
                                            "container_id": session.restored.container.id,
                                            "marker_absent_before_write": True,
                                            "cleanup_confirmed": True})
                client.ping()  # A caller-owned client is still usable after exit.

            for fault in ("C40", "missing", "digest", "json"):
                print(f"Checking rejection: {fault}", flush=True)
                candidate = (select_resume_start(source, "C40", model="M", max_steps=2)
                             if fault == "C40" else corrupt_copy(start, output, fault))
                count = len(workspace_starts)
                destination = output / ("reject-" + fault)
                try:
                    with restore_resume_start(candidate, destination, client):
                        raise AssertionError(f"{fault} unexpectedly delivered")
                except (IntegrityError, RecordUsageError) as error:
                    expected_error = {"C40": "Submitted", "missing": "missing or unreadable",
                                      "digest": "hash mismatch", "json": "not valid JSON"}[fault]
                    assert expected_error in str(error), str(error)
                    report = checked_report(client, destination)
                    assert len(workspace_starts) == count
                    assert not (destination / "restore-report.json").exists()
                    assert report["caller"]["status"] == "not_started"
                    summary["rejections"][fault] = {"error_type": type(error).__name__,
                                                    "error": str(error), "before_task_container": True,
                                                    "report": str(destination)}

            print("Checking managed-file R1 difference", flush=True)
            def inject(materials, executor):
                check(executor, actions, "managed_content_difference",
                      f"printf '\\n# Resume controlled content difference\\n' >> {MANAGED_FILE}")
                return r1(materials, executor)
            destination = output / "reject-r1"
            with patch("palinreplay.resume.validation.validate_r1", inject):
                try:
                    with restore_resume_start(start, destination, client):
                        raise AssertionError("R1 mismatch unexpectedly delivered")
                except IntegrityError as error:
                    assert MANAGED_FILE.lstrip("/") in str(error)
                    report = checked_report(client, destination)
                    assert report["r1"]["status"] == "failed" and report["r1"]["scope"] == SCOPE
                    summary["rejections"]["R1"] = {"error": str(error), "report": str(destination),
                                                   "delivered": False, "cleanup_confirmed": True}
            assert len(workspace_starts) == len(summary["r1_calls"]) == 3
            assert len({e["container_id"] for e in summary["r1_calls"]}) == 3
            for restored, verified in zip(summary["restores"], summary["r1_calls"][:2], strict=True):
                assert restored["container_id"] == verified["container_id"]
            assert len([e for e in operations if e["op"] == "verify"]) == 3
            summary["pool_after"] = pool_audit(client, materials.snapshot)
            assert summary["pool_before"]["source"] == summary["pool_after"]["source"]
            assert summary["pool_before"]["workspaces"] == summary["pool_after"]["workspaces"]
            assert not any(Path(w).name in summary["pool_after"]["workspaces"] for w in workspace_starts)
            assert not client.containers.list(all=True, filters={"label": OWNER_LABEL})
            assert not client.volumes.list(filters={"label": OWNER_LABEL})
            nodes_after = node_inventory(start.source_store)
            write_json(output / "source-nodes-after.json", nodes_after)
            assert nodes_after == nodes_before
            assert not any(counts.values()), counts
            summary.update(status="passed", node_id=start.node.node_id, run_id=start.node.run_id,
                           state_id=start.node.state_id, marker=marker, node_records_unchanged=True,
                           source_snapshot_unchanged=True, persistent_pool_retained=True,
                           temporary_resources_released=True, r1_once_per_container=True)
    except BaseException as error:
        summary.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        if client is not None:
            client.close()
        after = file_inventory(recording)
        write_json(output / "source-files-after.json", after)
        summary["source_files_unchanged"] = before == after
        if before != after:
            summary["status"] = "failed"
        summary["source_file_count"] = len(before)
        summary["duration_seconds"] = time.monotonic() - started
        write_json(output / "pool-operations.json", operations)
        write_json(output / "acceptance.json", summary, replace=True)
    assert summary["status"] == "passed", summary
    print(json.dumps({k: summary[k] for k in ("status", "counts", "source_files_unchanged",
                                              "temporary_resources_released", "duration_seconds")}))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="new directory outside recording")
    arguments = parser.parse_args()
    main(arguments.source, arguments.output)
