"""Own one Replay's lifetime and retain reports on every exit path."""

from pathlib import Path

from palinreplay.core.errors import IntegrityError
from palinreplay.core.models import Failure, RunEnd
from palinreplay.core.progress import ProgressCallback, ProgressEvent, ProgressReporter
from palinreplay.core.replay_outcome import failure_evidence
from palinreplay.record.restore import load_restore_materials, restore_r1_node
from palinreplay.replay.context import ReplayContext
from palinreplay.replay.initial import initial_recorder
from palinreplay.replay.materials import load_replay_materials
from palinreplay.replay.plan import pin_replay
from palinreplay.replay.report import exception_evidence, execution_facts, final_execution_status
from palinreplay.replay.run import replay_range
from palinreplay.replay.stats import ReplayStats
from palinreplay.restoration.environment import RestoredExecutor
from palinreplay.restoration.service_config import load_service_input, resolve_services
from palinreplay.restoration.services import PreparationJournal, prepare_services
from palinreplay.restoration.validation import validate_r1
from palinreplay.sandbox.client import connect_docker, docker_endpoint
from palinreplay.storage.btrfs.models import BACKEND, BtrfsRuntime, Snapshot
from palinreplay.storage.btrfs.verification import capture_info
from palinreplay.storage.io import outside_store, write_json
from palinreplay.storage.replay_finalization import PROTOCOL, publish, receipt_path
from palinreplay.storage.store import RecordStore


def run_replay(
    source, start, end=None, *, run_id=None, output, client=None,
    progress: ProgressCallback | None = None, services_config: str | Path | None = None,
):
    service_input = load_service_input(services_config) if services_config is not None else None
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
    report["outcome_evidence"] = {"faults": [], "cancellations": []}
    closes, publications = {}, {}
    binding = None

    def remember(error):
        for key, values in exception_evidence(error).items():
            report["outcome_evidence"][key].extend(values)

    def persist_fault(phase, error, *, category="cleanup", resource=None):
        remember(error)
        # Cleanup and publication interrupts are also independent framework faults.
        report["outcome_evidence"]["faults"].append({
            "phase": phase, "resource": resource,
            "error_type": type(error).__name__, "error": str(error),
        })
        if store is not None and recorder is not None and not store._closed:
            try:
                store.save(Failure(
                    run_id=recorder.run.run_id, attempt_id=recorder.attempt.attempt_id,
                    category=category, details=store.put_json({
                        "phase": phase, "resource": resource,
                        "error_type": type(error).__name__, "error": str(error),
                    }),
                ))
            except BaseException as diagnostic:
                remember(diagnostic)
                report.setdefault("diagnostic_errors", []).append(
                    f"{type(diagnostic).__name__}: {diagnostic}"
                )

    def write_report():
        report["execution_status"] = final_execution_status(report)
        try:
            write_json(output / "report.json", report, replace=True)
        except BaseException as error:
            publications["report.json"] = False
            report.setdefault("report_errors", []).append(f"report.json: {type(error).__name__}: {error}")
            persist_fault("report-persistence", error, resource="report.json")
            return False
        else:
            publications.setdefault("report.json", True)
            return True

    def cancelled_during_cleanup(error, *, persist=True):
        nonlocal primary
        remember(error)
        detail = {"phase": "replay-cleanup", "error_type": type(error).__name__, "error": str(error)}
        report.setdefault("cancellation", detail)
        if primary is None:
            primary = error
            report.update(error_type=type(error).__name__, error=str(error))
        if persist and store is not None and recorder is not None:
            try:
                store.save(Failure(run_id=recorder.run.run_id, attempt_id=recorder.attempt.attempt_id,
                                   category="execution", details=store.put_json(detail)))
            except BaseException as diagnostic:
                remember(diagnostic)
                report["termination_error"] = f"{type(diagnostic).__name__}: {diagnostic}"

    try:
        notify(ProgressEvent("preparing"))
        with RecordStore.open(source) as origin, origin.snapshot():
            plan = pin_replay(origin, start, end, run_id=run_id)
            services = resolve_services(service_input, origin, plan)
            if service_input is not None and service_input.manifest is not None:
                report["services_manifest"] = {
                    "path": str(service_input.path),
                    "source": service_input.manifest.source.model_dump(mode="json"),
                    "selected": [service.name for service in services.services],
                }
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
        executor.pool.verify(baseline.snapshot)
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
        recorder = initial_recorder(store, materials, executor, context.export,
                                    r1=report["r1"], preparation=preparation)
        report["run_id"] = recorder.run.run_id
        report["finalization"] = {
            "protocol": PROTOCOL, "confirmation": "receipt-required",
            "receipt": "record/" + receipt_path(store.root, recorder.attempt).name,
        }
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
        remember(error)
        report.update(execution_status="cancelled" if isinstance(error, KeyboardInterrupt) else "failed",
                      error_type=type(error).__name__, error=str(error))
        if isinstance(error, KeyboardInterrupt):
            report["cancellation"] = {"phase": report["phase"], "error_type": type(error).__name__,
                                      "error": str(error)}
        if error.__cause__ is not None:
            report["cause"] = {"error_type": type(error.__cause__).__name__,
                               "error": str(error.__cause__)}
        if journal is not None and journal.data["status"] == "preparing":
            journal.data.update(status="failed", error_type=type(error).__name__, error=str(error))
            try:
                journal.save()
            except BaseException as diagnostic:
                remember(diagnostic)
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
                remember(diagnostic)
                report["termination_error"] = f"{type(diagnostic).__name__}: {diagnostic}"
                error.add_note(f"record termination also failed: {diagnostic!r}")
    finally:
        try:
            notify(ProgressEvent("cleanup"))
        except KeyboardInterrupt as error:
            cancelled_during_cleanup(error)
        finally:
            for name, resource in (("recorder", recorder), ("restore", restored),
                                   ("client", client if own_client else None)):
                if resource is None:
                    continue
                try:
                    resource.close()
                    closes[name] = True
                except BaseException as error:
                    closes[name] = False
                    report["cleanup_errors"].append(f"{name}: {type(error).__name__}: {error}")
                    if isinstance(error, KeyboardInterrupt):
                        cancelled_during_cleanup(error)
                    persist_fault("replay-cleanup", error, resource=name)
            if recorder is not None:
                try:
                    facts = execution_facts(recorder)
                    for key, values in facts.pop("outcome_evidence").items():
                        report["outcome_evidence"][key].extend(values)
                    report.update(facts)
                    stats.data["historical_answer_supplies"] = report["history_usage"]["historical_answer_supplies"]
                except BaseException as error:
                    report["record_report_error"] = f"{type(error).__name__}: {error}"
                    persist_fault("record-report", error)
            if primary is not None:
                notes = [*getattr(primary, "__notes__", ()),
                         *getattr(primary.__cause__, "__notes__", ())]
                if notes:
                    report["error_notes"] = notes
            report["cleanup_confirmed"] = not report["cleanup_errors"]
            if "r1" in report:
                try:
                    write_json(output / "r1-validation.json", report["r1"])
                    publications["r1-validation.json"] = True
                except BaseException as error:
                    publications["r1-validation.json"] = False
                    report.setdefault("report_errors", []).append(
                        f"r1-validation.json: {type(error).__name__}: {error}"
                    )
                    persist_fault("report-persistence", error, resource="r1-validation.json")
            write_report()
            if recorder is not None:
                try:
                    ends = store.list(RunEnd, run_id=recorder.run.run_id)
                    binding = {
                        "schema_version": "palinreplay.replay-finalization.v1",
                        "run_id": recorder.run.run_id, "config": recorder.run.config,
                        "attempt": recorder.attempt, "run_end": ends[-1] if ends else None,
                        "failure_ids": [f.failure_id for f in store.list(Failure, run_id=recorder.run.run_id)],
                    }
                except BaseException as error:
                    persist_fault("finalization-binding", error)
            if store is not None:
                try:
                    store.close()
                    closes["store"] = True
                except BaseException as error:
                    closes["store"] = False
                    report["cleanup_errors"].append(f"store: {type(error).__name__}: {error}")
                    if isinstance(error, KeyboardInterrupt):
                        cancelled_during_cleanup(error, persist=False)
                    persist_fault("replay-cleanup", error, resource="store")
                    if not store._closed:
                        try:
                            if binding is not None:
                                binding["failure_ids"] = [f.failure_id for f in store.list(
                                    Failure, run_id=recorder.run.run_id)]
                        except BaseException as diagnostic:
                            remember(diagnostic)
                        try:
                            # One final close of the existing connection, no new SQLite writer.
                            store._db.close()
                            store._closed = True
                        except BaseException as diagnostic:
                            remember(diagnostic)
            report["cleanup_confirmed"] = not report["cleanup_errors"]
            if not write_report():
                # One bounded update can expose a transient write fault in the
                # external report; prior publication failures remain failures.
                write_report()
            report["execution_status"] = final_execution_status(report)
            if binding is not None:
                try:
                    payload = {**binding, "execution_status": report["execution_status"],
                               **report["outcome_evidence"], "resource_closes": closes,
                               "publications": publications}
                    publication = publish(store.root, payload)
                except BaseException as error:
                    remember(error)
                    publication = {
                        "status": "unconfirmed", "errors": [f"{type(error).__name__}: {error}"],
                        "failure": {"phase": "receipt-publication",
                                    "error_type": type(error).__name__, "error": str(error)},
                    }
                if publication["status"] != "confirmed":
                    detail = publication.get("failure", {
                        "phase": "receipt-publication", "error_type": "ReceiptPublicationError",
                        "error": "required receipt publication was not confirmed",
                    })
                    evidence = failure_evidence("cleanup", detail)
                    for key, values in evidence.items():
                        report["outcome_evidence"][key].extend(values)
                    if evidence["cancellations"]:
                        report.setdefault("cancellation", detail)
                    report["receipt_publication"] = publication
                    report.setdefault("report_errors", []).extend(publication.get("errors", ()))
                    report["execution_status"] = "failed"
                    report["finalization"]["confirmation"] = "unconfirmed"
                    # A failed publication has no confirmed terminal receipt.
                    write_report()
    report["execution_status"] = final_execution_status(report)
    try:
        notify(ProgressEvent("finished", status=report["execution_status"],
                             label=report["execution_status"].upper()))
    except KeyboardInterrupt:
        # Resources and the final report are already closed/published. A terminal
        # observer cannot replace the durable outcome after this boundary.
        pass
    if primary is not None and not isinstance(primary, Exception):
        if not isinstance(primary, KeyboardInterrupt) or report["execution_status"] == "cancelled":
            raise primary
    return report
