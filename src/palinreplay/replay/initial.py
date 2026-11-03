"""Initialize an independent recording at the actual restored boundary."""

from contextlib import nullcontext

from palinreplay.core.errors import IntegrityError
from palinreplay.core.interfaces import AgentSnapshot, BoundaryAdapter, Payload
from palinreplay.record.recorder import Recorder
from palinreplay.record.state import BtrfsStateBackend
from palinreplay.replay.config import replay_config
from palinreplay.storage.btrfs.models import Snapshot
from palinreplay.storage.rootfs import SCOPE


def validate_r1(materials, executor, *, stats=None):
    """Validate the restored prefix and files before preparation or history execution."""
    restored = executor.restored
    result = {"status": "checking", "context_checks": {
        "context": restored.context == materials.context,
        "input_position": restored.input_position == materials.input_position,
        "config": restored.config == materials.config,
    }}
    try:
        if not all(result["context_checks"].values()):
            raise IntegrityError("restored context/input position/config differs from source prefix")
        source = Snapshot.model_validate(restored.report["snapshot"])
        executor._container.pause()
        try:
            with stats.timed("content_compare") if stats else nullcontext():
                result["files"] = executor.pool.command(
                    "verify", source=source.name, workspace=executor.workspace,
                    scope=SCOPE, oracle_sha256=executor.runtime.c0_oracle_sha256,
                )
        finally:
            executor._container.unpause()
        restored.report["checks"]["full_filesystem"] = True
        restored.report.update(verification="full-logical-filesystem", file_comparison=result["files"])
        restored._write_report()
        result["status"] = "passed"
        return result
    except BaseException as error:
        result.update(status="failed", error_type=type(error).__name__, error=str(error))
        error.r1_report = result
        raise


def initial_recorder(
    store, materials, executor, export_agent=None, *, stats=None, validation_mode="lenient",
    preparation=None,
):
    config = replay_config(materials, preparation=preparation)
    if export_agent is None:
        initial = AgentSnapshot(Payload.json(materials.context), Payload.json(materials.input_position))
        def export_agent(boundary):
            return initial
    backend = BtrfsStateBackend(executor, public_env_keys=executor.runtime.launch.environment,
                                runtime=executor.runtime)
    recorder = Recorder(store, task_id=materials.plan.run.task_id,
                        agent_name=materials.plan.run.agent_name,
                        agent_version=materials.plan.run.agent_version,
                        config=Payload.json(config),
                        boundary=BoundaryAdapter(export_agent, backend.capture))
    try:
        recorder.start_from_source()
        return recorder
    except BaseException as error:
        recorder.__exit__(type(error), error, error.__traceback__)
        raise
