"""Managed restoration at a saved boundary, before any new Agent decisions."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from palinreplay.record.restore import RestoredNode, RestoreMaterials
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.replay.services import PreparationJournal, prepare_services
from palinreplay.resume.context import ResumePrefix, validate_resume_context
from palinreplay.resume.environment import restore_environment, saved_execution_config
from palinreplay.resume.materials import load_resume_materials
from palinreplay.resume.report import exception_evidence, finish_report, new_report
from palinreplay.resume.services import load_resume_service_input, resolve_resume_services
from palinreplay.resume.start import ResumeStart
from palinreplay.resume.validation import validate_workspace
from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.io import outside_store, write_json


@dataclass(frozen=True)
class ResumeSession:
    start: ResumeStart
    materials: RestoreMaterials
    restored: RestoredNode
    executor: RestoredExecutor
    prefix: ResumePrefix
    output: Path
    report: dict

    @property
    def report_path(self):
        return self.output / "report.json"

    @property
    def context(self):
        return self.prefix.context

    @property
    def input_position(self):
        return self.prefix.input_position


def prepare_output(start, output):
    if output is None:
        name = datetime.now().strftime("%y%m%d_%H%M%S") + f"_C{start.node.index}-restore"
        output = Path.cwd() / "outputs" / "resume" / name
    output = Path(output).absolute()
    source = start.source_store.resolve()
    # The recording's siblings of record/ are immutable source evidence too.
    recording = source.parent if source.name == "record" else source
    outside_store(recording, output)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    return output


@contextmanager
def restore_resume_start(start: ResumeStart, output=None, client=None, *, services_config=None):
    """Yield after R1 and optional service preparation; own only newly created resources."""
    output = prepare_output(start, output)
    own_client = client is None
    report = new_report(start, output, own_client)
    restored = executor = journal = primary = None
    delivered = False
    try:
        if services_config is not None:
            report["phase"] = "services"
        services = resolve_resume_services(start, load_resume_service_input(services_config))
        report.update(phase="materials", restore_status="in_progress")
        if services is None:
            report["preparation"] = {"status": "skipped"}
        materials = load_resume_materials(start)
        report["phase"] = "context"
        prefix = validate_resume_context(materials)
        execution = saved_execution_config(materials)
        report.update(
            phase="restore", execution_config=execution,
            source_records={"run": materials.run.model_dump(mode="json"),
                            "node": materials.node.model_dump(mode="json"),
                            "state": materials.state.model_dump(mode="json")},
            prefix={"message_count": len(prefix.messages), "counters": prefix.counters,
                    "next_message": prefix.input_position["next_message"]},
        )
        write_json(output / "report.json", report, replace=True)
        if own_client:
            client = connect_docker(600)
        restored, executor = restore_environment(
            start, materials, client=client, report_path=output / "restore-report.json",
        )
        report["phase"] = "validate_r1"
        report["r1"] = validate_workspace(materials, executor)
        report["restore_status"] = "passed"
        if services is not None:
            report["phase"] = "service_preparation"
            services = services.model_copy(update={"services": tuple(
                service.model_copy(update={"cwd": service.cwd or execution["cwd"]})
                for service in services.services
            )})
            report["preparation_report_path"] = str(output / "preparation.json")
            report["preparation"] = {"status": "preparing"}
            journal = PreparationJournal(output / "preparation.json", services)
            report["preparation"] = journal.data
            prepare_services(executor, services, journal)
        report.update(status="ready", phase="delivery")
        write_json(output / "report.json", report, replace=True)
        delivered = True
        report.update(phase="caller", caller={"status": "running"})
        yield ResumeSession(start, materials, restored, executor, prefix, output, report)
        report["caller"] = {"status": "completed"}
    except BaseException as error:
        primary = error
        if delivered:
            report["caller"] = {"status": "failed", **exception_evidence(error)}
        elif report["phase"] == "services":
            report["services_error"] = exception_evidence(error)
        elif report["phase"] == "service_preparation":
            report["preparation_error"] = exception_evidence(error)
            if report["preparation"]["status"] != "failed":
                report["preparation"]["status"] = "failed"
                if journal is not None:
                    journal.save(primary=error)
        elif report["restore_status"] != "passed":
            report.update(restore_status="failed", restore_error={
                "phase": report["phase"], **exception_evidence(error),
            })
        else:
            report["delivery_error"] = exception_evidence(error)
        if report["restore_status"] == "failed":
            if hasattr(error, "r1_report"):
                report["r1"] = error.r1_report
            if hasattr(error, "restore_report"):
                report["resources"] = error.restore_report
        raise
    finally:
        finish_report(report, output, restored=restored, executor=executor, client=client,
                      own_client=own_client, primary=primary)
