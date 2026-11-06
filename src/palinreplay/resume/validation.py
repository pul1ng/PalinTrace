"""One R1 comparison on the same container that will be handed to the caller."""

from copy import deepcopy

from palinreplay.restoration.validation import validate_r1
from palinreplay.storage.rootfs import SCOPE


def validate_workspace(materials, executor):
    restored = executor.restored
    scope = {"scope": deepcopy(SCOPE), "capture": materials.state.capture.model_dump(mode="json")}
    try:
        result = validate_r1(materials, executor) | scope
    except BaseException as error:
        result = getattr(error, "r1_report", {
            "status": "failed", "error_type": type(error).__name__, "error": str(error),
        }) | scope
        error.r1_report = result
        restored.report.update(r1=result, status="failed", phase="validate_r1",
                               error_type=type(error).__name__, error_message=str(error))
        try:
            restored._write_report()
        except BaseException as diagnostic:
            error.add_note(f"R1 evidence write also failed: {diagnostic!r}")
        raise
    restored.report.update(r1=result, status="verified", phase="complete")
    restored._write_report()
    return result
