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
from palinreplay.resume.start import ResumeStart
from palinreplay.resume.validation import validate_workspace
from palinreplay.sandbox.base import SandboxCleanupError
from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.io import outside_store


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
    restored = executor = primary = None
    try:
        materials = load_resume_materials(start)
        prefix = validate_resume_context(materials)
        saved_execution_config(materials)
        if own_client:
            client = connect_docker(600)
        restored, executor = restore_environment(
            start, materials, client=client, report_path=output / "restore-report.json",
        )
        validate_workspace(materials, executor)
        yield ResumeSession(start, materials, restored, executor, prefix, output, restored.report)
    except BaseException as error:
        primary = error
        raise
    finally:
        errors = []
        for name, resource in (("executor", executor), ("restore", restored),
                               ("client", client if own_client else None)):
            if resource is not None:
                try:
                    resource.close()
                except BaseException as error:
                    errors.append(f"{name}: {type(error).__name__}: {error}")
        if errors:
            if primary is not None:
                primary.add_note("Resume cleanup also failed: " + "; ".join(errors))
            else:
                raise SandboxCleanupError("; ".join(errors))
