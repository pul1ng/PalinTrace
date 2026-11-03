"""Own one Replay's lifetime and retain reports on every exit path."""

from pathlib import Path

from palinreplay.core.errors import IntegrityError
from palinreplay.core.models import Failure
from palinreplay.core.progress import ProgressCallback, ProgressEvent, ProgressReporter
from palinreplay.record.restore import load_restore_materials, restore_r1_node
from palinreplay.replay.context import ReplayContext
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.replay.initial import initial_recorder, validate_r1
from palinreplay.replay.materials import load_replay_materials
from palinreplay.replay.plan import pin_replay
from palinreplay.replay.report import execution_facts
from palinreplay.replay.run import replay_range
from palinreplay.replay.service_config import load_services
from palinreplay.replay.services import PreparationJournal, prepare_services
from palinreplay.replay.stats import ReplayStats
from palinreplay.sandbox.client import connect_docker, docker_endpoint
from palinreplay.storage.btrfs.models import BACKEND, BtrfsRuntime, Snapshot
from palinreplay.storage.btrfs.verification import capture_info
from palinreplay.storage.io import outside_store, write_json
from palinreplay.storage.store import RecordStore


def run_replay(
    source, start, end=None, *, run_id=None, output, client=None,
    progress: ProgressCallback | None = None, services_config: str | Path | None = None,
):
    services = load_services(services_config) if services_config is not None else None
    source, output = Path(source).absolute(), Path(output).absolute()
    outside_store(source, output)
    if source.resolve().is_relative_to(output.resolve()):
        raise ValueError("output cannot contain the source Record store")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    report = {"schema_version": "palinreplay.replay-report.v2", "execution_status": "failed",
              "initial_state_mode": "source-reference-v1", "node_numbering": "source-index-v1",
              "execution_policy": "historical-exit-code-v1", "preparation_state_captured": False,
              "actual_node_count": 0, "completed_steps": 0, "mappings": [], "steps": [],
              "incomplete_step": False, "incomplete_steps": [], "last_node": None,
              "phase": "preflight", "source_store": str(source), "output": str(output),
              "cleanup_confirmed": False, "cleanup_errors": []}
    stats = ReplayStats()
    report["statistics"] = stats.data
    write_json(output / "report.json", report)
    restored = recorder = store = journal = None
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
        report["source_start"] = {
            "source_store": str(source), "run_id": plan.run.run_id, "index": plan.start.index,
            "node_id": plan.start.node_id, "state_id": plan.start.state_id, "reference": True,
        }
        report["source_start_index"], report["source_end_index"] = plan.start.index, plan.end.index
        report["selected_steps"] = len(materials.steps)
        context = ReplayContext(materials)
        if client is None:
            notify(ProgressEvent("connecting"))
            client = connect_docker(600)
        report["phase"] = "restore"
        notify(ProgressEvent("restoring", source_step=plan.start.index, completed_steps=0))
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
        report["phase"] = "r1-validation"
        notify(ProgressEvent("verifying", source_step=plan.start.index, completed_steps=0))
        report["r1"] = validate_r1(materials, executor, stats=stats)
        preparation = None
        if services is not None:
            report["phase"] = "service-preparation"
            journal = PreparationJournal(output / "preparation.json", services)
            report["preparation"] = journal.data
            notify(ProgressEvent("service-preparation"))
            with stats.timed("preparation"):
                preparation = prepare_services(executor, services, journal)
        store = RecordStore.create(output / "record")
        report["phase"] = "source-initialization"
        recorder = initial_recorder(store, materials, executor, context.export, preparation=preparation)
        report["run_id"] = recorder.run.run_id
        notify(ProgressEvent("source-reference", source_step=plan.start.index,
                             completed_steps=0, total_steps=len(materials.steps)))
        report["phase"] = "execute"
        replay_range(recorder, context, executor, materials, report=report, stats=stats,
                     progress=notify)
        if recorder.phase == "ready":
            recorder.finish("Replay " + report["execution_status"], details={"replay": report})
        report["phase"] = "finished"
    except BaseException as error:
        primary = error
        report.update(execution_status="cancelled" if isinstance(error, KeyboardInterrupt) else "failed",
                      error_type=type(error).__name__, error=str(error))
        if error.__cause__ is not None:
            report["cause"] = {"error_type": type(error.__cause__).__name__,
                               "error": str(error.__cause__)}
        if journal is not None and journal.data["status"] == "preparing":
            journal.data.update(status="failed", error_type=type(error).__name__, error=str(error))
            try:
                journal.save()
            except BaseException as diagnostic:
                report["preparation_error"] = f"{type(diagnostic).__name__}: {diagnostic}"
                error.add_note(f"preparation evidence could not be saved: {diagnostic!r}")
        if hasattr(error, "r1_report"):
            report["r1"] = error.r1_report
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
            for name, resource in (("recorder", recorder), ("restore", restored),
                                   ("client", client if own_client else None), ("store", store)):
                if resource is not None:
                    if name == "store" and recorder is not None:
                        try:
                            report.update(execution_facts(recorder))
                        except BaseException as error:
                            report["record_report_error"] = f"{type(error).__name__}: {error}"
                            report.update(execution_status="failed")
                    try:
                        resource.close()
                    except BaseException as error:
                        report["cleanup_errors"].append(f"{name}: {type(error).__name__}: {error}")
                        if store is not None and recorder is not None and name != "store":
                            try:
                                store.save(Failure(
                                    run_id=recorder.run.run_id, attempt_id=recorder.attempt.attempt_id,
                                    category="cleanup", details=store.put_json({
                                        "phase": "replay-cleanup", "resource": name,
                                        "error_type": type(error).__name__, "error": str(error),
                                    }),
                                ))
                            except BaseException as diagnostic:
                                report["cleanup_errors"].append(
                                    f"cleanup-diagnostic: {type(diagnostic).__name__}: {diagnostic}"
                                )
            report["cleanup_confirmed"] = not report["cleanup_errors"]
            if report["cleanup_errors"]:
                report.update(execution_status="failed")
                report.setdefault("error", "resource cleanup failed")
            if primary is not None:
                notes = [*getattr(primary, "__notes__", ()),
                         *getattr(primary.__cause__, "__notes__", ())]
                if notes:
                    report["error_notes"] = notes
            documents = [(output / "report.json", report)]
            if "r1" in report:
                documents.insert(0, (output / "r1-validation.json", report["r1"]))
            for path, document in documents:
                try:
                    write_json(path, document, replace=path.name == "report.json")
                except BaseException as error:
                    report.setdefault("report_errors", []).append(
                        f"{path.name}: {type(error).__name__}: {error}"
                    )
                    report.update(execution_status="failed")
                    report.setdefault("error", "report persistence failed")
                    if primary is None:
                        primary = error
    if primary is not None and not isinstance(primary, Exception):
        notify(ProgressEvent("finished",
                             status="cancelled" if isinstance(primary, KeyboardInterrupt) else "failed"))
        raise primary
    notify(ProgressEvent("finished", status=report["execution_status"],
                         label=report["execution_status"].upper()))
    return report
