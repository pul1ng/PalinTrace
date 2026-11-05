"""Managed restoration at a saved boundary, before any new Agent decisions."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from palinreplay.record.restore import RestoredNode, RestoreMaterials
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.resume.context import ResumePrefix, validate_resume_context
from palinreplay.resume.environment import restore_environment, saved_execution_config
from palinreplay.resume.materials import load_resume_materials
from palinreplay.resume.report import exception_evidence, finish_report, new_report
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
def restore_resume_start(start: ResumeStart, output=None, client=None):
    """Yield only after R1 passes; own new resources and only an internally created client."""
    output = prepare_output(start, output)
    own_client = client is None
    report = new_report(start, output, own_client)
    restored = executor = primary = None
    delivered = False
    try:
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
        report.update(restore_status="passed", status="ready", phase="delivery")
        write_json(output / "report.json", report, replace=True)
        delivered = True
        report.update(phase="caller", caller={"status": "running"})
        yield ResumeSession(start, materials, restored, executor, prefix, output, report)
        report["caller"] = {"status": "completed"}
    except BaseException as error:
        primary = error
        if delivered:
            report["caller"] = {"status": "failed", **exception_evidence(error)}
        else:
            report.update(restore_status="failed", restore_error={
                "phase": report["phase"], **exception_evidence(error),
            })
        if hasattr(error, "r1_report"):
            report["r1"] = error.r1_report
        if hasattr(error, "restore_report"):
            report["resources"] = error.restore_report
        raise
    finally:
        finish_report(report, output, restored=restored, executor=executor, client=client,
                      own_client=own_client, primary=primary)
