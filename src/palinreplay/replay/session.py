"""Own one Replay's lifetime and retain reports on every exit path."""

from pathlib import Path

from palinreplay.record.restore import load_restore_materials, restore_r1_node
from palinreplay.replay.context import ReplayContext
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.replay.initial import initial_recorder
from palinreplay.replay.materials import load_replay_materials
from palinreplay.replay.plan import pin_replay
from palinreplay.replay.run import btrfs_comparison, replay_range
from palinreplay.sandbox.client import connect_docker, docker_endpoint
from palinreplay.storage.btrfs.models import Snapshot
from palinreplay.storage.io import outside_store, write_json
from palinreplay.storage.store import RecordStore


def run_replay(source, start, end=None, *, run_id=None, output, client=None):
    source, output = Path(source).absolute(), Path(output).absolute()
    outside_store(source, output)
    if source.resolve().is_relative_to(output.resolve()):
        raise ValueError("output cannot contain the source Record store")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    report = {"schema_version": "palinreplay.replay-report.v1", "status": "failed",
              "phase": "preflight", "source_store": str(source), "output": str(output),
              "cleanup_confirmed": False, "cleanup_errors": []}
    write_json(output / "report.json", report)
    restored = recorder = store = None
    own_client = client is None
    primary = None
    try:
        with RecordStore.open(source) as origin, origin.snapshot():
            plan = pin_replay(origin, start, end, run_id=run_id)
            materials = load_replay_materials(origin, plan)
            baseline = load_restore_materials(origin, plan.start.node_id)
            snapshots = [Snapshot.model_validate(origin.read_json(state.manifest)) for state in plan.states]
        report["source"] = plan.provenance()
        context = ReplayContext(materials)
        if client is None:
            client = connect_docker(600)
        report["phase"] = "restore"
        with RecordStore.open(source) as origin:
            restored = restore_r1_node(origin, plan.start.node_id, client=client,
                                       docker_host=docker_endpoint(),
                                       report_path=output / "restore.json")
        executor = RestoredExecutor(restored, baseline.runtime)
        for history in materials.steps:
            for tool in history.tools:
                if tuple(tool.request["interpreter"]) != tuple(executor.runtime.launch.shell):
                    raise ValueError("historical interpreter differs from the restored launch shell")
        store = RecordStore.create(output / "record")
        report["phase"] = "initial-boundary"
        recorder = initial_recorder(store, materials, executor, context.export)
        report["run_id"] = recorder.run.run_id
        report["phase"] = "execute"
        replay_range(recorder, context, executor, materials,
                     btrfs_comparison(store, executor, snapshots), report=report)
        if recorder.phase == "ready":
            recorder.finish("Replay " + report["status"], details={"replay": report})
        report["phase"] = "finished"
    except BaseException as error:
        primary = error
        report.update(status="failed", error_type=type(error).__name__, error=str(error))
        if recorder is not None and recorder.phase not in ("stopped", "closed"):
            try:
                recorder.__exit__(type(error), error, error.__traceback__)
            except BaseException as diagnostic:
                error.add_note(f"record termination also failed: {diagnostic!r}")
    finally:
        for name, resource in (("recorder", recorder), ("restore", restored), ("store", store),
                               ("client", client if own_client else None)):
            if resource is not None:
                try:
                    resource.close()
                except BaseException as error:
                    report["cleanup_errors"].append(f"{name}: {type(error).__name__}: {error}")
        report["cleanup_confirmed"] = not report["cleanup_errors"]
        if report["cleanup_errors"] and report["status"] == "match":
            report.update(status="failed", error="resource cleanup failed")
        write_json(output / "report.json", report, replace=True)
    if primary is not None and not isinstance(primary, Exception):
        raise primary
    return report
