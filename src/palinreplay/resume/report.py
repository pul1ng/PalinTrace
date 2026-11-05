"""Resume evidence is separate from the immutable source and the R1 resource report."""

from palinreplay.sandbox.base import SandboxCleanupError
from palinreplay.storage.io import write_json


def exception_evidence(error):
    return {"error_type": type(error).__name__, "error": str(error),
            "notes": list(getattr(error, "__notes__", []))}


def new_report(start, output, own_client):
    report = {
        "schema_version": "palinreplay.resume-restore.v1",
        "status": "in_progress", "phase": "materials", "restore_status": "not_started",
        "source_store": str(start.source_store), "node": start.node.model_dump(mode="json"),
        "new_decisions": {"model": start.model, "max_steps": start.max_steps},
        "output": str(output), "restore_report_path": str(output / "restore-report.json"),
        "caller": {"status": "not_started"}, "r1": {"status": "not_run"},
        "preparation": {"status": "not_started"}, "preparation_report_path": None,
        "client_ownership": "session" if own_client else "caller",
        "cleanup_confirmed": False, "cleanup_errors": [], "cleanup": {},
    }
    write_json(output / "report.json", report)
    return report


def finish_report(report, output, *, restored, executor, client, own_client, primary):
    errors = report["cleanup_errors"]
    for name, resource in (("executor", executor), ("restore", restored),
                           ("client", client if own_client else None)):
        status = "not_created" if name != "client" or own_client else "caller_managed"
        if resource is not None:
            try:
                resource.close()
                status = "closed"
            except BaseException as error:
                status = "failed"
                errors.append({"resource": name, **exception_evidence(error)})
        report["cleanup"][name] = status
    resources = restored.report if restored is not None else report.get("resources")
    if resources is not None:
        report["resources"] = resources
        if restored is None:
            report["cleanup"]["restore"] = (
                "closed_by_restorer" if resources.get("cleanup_confirmed") else "failed"
            )
        if not resources.get("cleanup_confirmed", False):
            errors.append({"resource": "restore", "error": "resource cleanup unconfirmed",
                           "details": resources.get("cleanup_errors", [])})
    report["cleanup_confirmed"] = not errors
    report["status"] = (
        "passed" if report["restore_status"] == "passed"
        and report["preparation"]["status"] in ("ready", "skipped")
        and report["caller"]["status"] == "completed" and not errors else "failed"
    )
    report["phase"] = "complete"
    pending = primary
    if errors:
        if pending is None:
            pending = SandboxCleanupError(f"Resume cleanup unconfirmed: {errors}")
        else:
            pending.add_note(f"Resume cleanup also failed: {errors}")
    try:
        write_json(output / "report.json", report, replace=True)
    except BaseException as error:
        report["status"] = "failed"
        report["report_error"] = exception_evidence(error)
        if pending is None:
            pending = error
        else:
            pending.add_note(f"Resume report write also failed: {error!r}")
    if pending is not None:
        pending.resume_report = report
        pending.resume_report_path = output / "report.json"
        if primary is None:
            raise pending
