"""Validate the restored prefix and files before using a saved workspace."""

from contextlib import nullcontext

from palinreplay.core.errors import IntegrityError
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
        primary = None
        try:
            with stats.timed("r1_validation") if stats else nullcontext():
                result["files"] = executor.pool.command(
                    "verify", source=source.name, workspace=executor.workspace,
                    scope=SCOPE, oracle_sha256=executor.runtime.c0_oracle_sha256,
                )
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                executor._container.unpause()
            except BaseException as error:
                if primary is None:
                    raise
                primary.add_note(f"container unpause also failed: {error!r}")
        restored.report["checks"]["full_filesystem"] = True
        restored.report.update(verification="full-logical-filesystem", file_comparison=result["files"])
        restored._write_report()
        result["status"] = "passed"
        return result
    except BaseException as error:
        result.update(status="failed", error_type=type(error).__name__, error=str(error))
        error.r1_report = result
        raise
