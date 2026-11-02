"""Own one Replay's lifetime and retain reports on every exit path."""

from pathlib import Path

from palinreplay.core.errors import IntegrityError
from palinreplay.core.progress import ProgressCallback, ProgressEvent, ProgressReporter
from palinreplay.record.restore import load_restore_materials, restore_r1_node
from palinreplay.replay.context import ReplayContext
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.replay.initial import initial_recorder
from palinreplay.replay.materials import load_replay_materials
from palinreplay.replay.plan import pin_replay
from palinreplay.replay.run import btrfs_comparison, replay_range
from palinreplay.replay.stats import ReplayStats
from palinreplay.sandbox.client import connect_docker, docker_endpoint
from palinreplay.storage.btrfs.models import BACKEND, BtrfsRuntime, Snapshot
from palinreplay.storage.btrfs.verification import capture_info
from palinreplay.storage.io import outside_store, write_json
from palinreplay.storage.store import RecordStore


def run_replay(
    source, start, end=None, *, run_id=None, output, client=None, validation_mode="lenient",
    progress: ProgressCallback | None = None,
):
    if validation_mode not in ("strict", "lenient"):
        raise ValueError("validation_mode must be 'strict' or 'lenient'")
    source, output = Path(source).absolute(), Path(output).absolute()
    outside_store(source, output)
    if source.resolve().is_relative_to(output.resolve()):
        raise ValueError("output cannot contain the source Record store")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    report = {"schema_version": "palinreplay.replay-report.v1", "status": "failed",
              "execution_status": "failed", "validation_status": "incomplete",
              "validation_mode": validation_mode,
              "phase": "preflight", "source_store": str(source), "output": str(output),
              "cleanup_confirmed": False, "cleanup_errors": []}
    stats = ReplayStats()
    report["statistics"] = stats.data
    write_json(output / "report.json", report)
    restored = recorder = store = None
    own_client = client is None
    primary = None
    notify = ProgressReporter(progress)
    try:
        notify(ProgressEvent("preparing"))
        with RecordStore.open(source) as origin, origin.snapshot():
            plan = pin_replay(origin, start, end, run_id=run_id)
            notify(ProgressEvent("preparing", total_steps=len(plan.nodes) - 1))
            materials = load_replay_materials(origin, plan)
            baseline = load_restore_materials(origin, plan.start.node_id)
            snapshots = [Snapshot.model_validate(origin.read_json(state.manifest)) for state in plan.states]
            for state, snapshot in zip(plan.states, snapshots, strict=True):
                runtime = BtrfsRuntime.model_validate(origin.read_json(state.runtime))
                if (state.backend != BACKEND or state.capture != capture_info() or state.dependencies
                        or snapshot.location != baseline.snapshot.location
                        or runtime.launch.shell != baseline.runtime.launch.shell):
                    raise IntegrityError("unsupported historical snapshot scope or execution semantics")
                for ref in state.artifacts:
                    origin.verify_artifact(ref)
        stats.inherit(materials.context)
        report["source"] = plan.provenance()
        context = ReplayContext(materials)
        if client is None:
            notify(ProgressEvent("connecting"))
            client = connect_docker(600)
        report["phase"] = "restore"
        notify(ProgressEvent("restoring", source_step=plan.start.index, step=0))
        with stats.timed("restore"), RecordStore.open(source) as origin:
            restored = restore_r1_node(origin, plan.start.node_id, client=client,
                                       docker_host=docker_endpoint(),
                                       report_path=output / "restore.json")
        executor = RestoredExecutor(restored, baseline.runtime)
        for snapshot in snapshots:
            executor.pool.verify(snapshot)
        for history in materials.steps:
            for tool in history.tools:
                if tuple(tool.request["interpreter"]) != tuple(executor.runtime.launch.shell):
                    raise ValueError("historical interpreter differs from the restored launch shell")
        store = RecordStore.create(output / "record")
        report["phase"] = "initial-boundary"
        notify(ProgressEvent("verifying", source_step=plan.start.index, step=0))
        recorder = initial_recorder(store, materials, executor, context.export, stats=stats,
                                    validation_mode=validation_mode)
        report["run_id"] = recorder.run.run_id
        report["phase"] = "execute"
        replay_range(recorder, context, executor, materials,
                     btrfs_comparison(store, executor, snapshots), report=report, stats=stats,
                     validation_mode=validation_mode, progress=notify)
        if recorder.phase == "ready":
            recorder.finish("Replay " + report["status"], details={"replay": report})
        report["phase"] = "finished"
    except BaseException as error:
        primary = error
        report.update(status="failed", execution_status="failed",
                      error_type=type(error).__name__, error=str(error))
        report.pop("stop_reason", None)
        if hasattr(error, "restore_report"):
            report["restore_report"] = error.restore_report
            report["cleanup_errors"].extend(
                f"restore: {detail}" for detail in error.restore_report["cleanup_errors"]
            )
        if recorder is not None and recorder.phase not in ("stopped", "closed"):
            try:
                recorder.__exit__(type(error), error, error.__traceback__)
            except BaseException as diagnostic:
                report["termination_error"] = f"{type(diagnostic).__name__}: {diagnostic}"
                error.add_note(f"record termination also failed: {diagnostic!r}")
    finally:
        try:
            notify(ProgressEvent("cleanup"))
        finally:
            for name, resource in (("recorder", recorder), ("restore", restored), ("store", store),
                                   ("client", client if own_client else None)):
                if resource is not None:
                    try:
                        resource.close()
                    except BaseException as error:
                        report["cleanup_errors"].append(f"{name}: {type(error).__name__}: {error}")
            report["cleanup_confirmed"] = not report["cleanup_errors"]
            if report["cleanup_errors"]:
                report.update(status="failed", execution_status="failed")
                report.setdefault("error", "resource cleanup failed")
                report.pop("stop_reason", None)
            write_json(output / "report.json", report, replace=True)
    if primary is not None and not isinstance(primary, Exception):
        notify(ProgressEvent("finished",
                             status="cancelled" if isinstance(primary, KeyboardInterrupt) else "failed"))
        raise primary
    notify(ProgressEvent("finished", status=report["execution_status"],
                         label=f"{report['execution_status'].upper()} / {report['validation_status'].upper()}"))
    return report
