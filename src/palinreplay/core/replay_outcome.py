"""One outcome rule for live Replay reports and persisted evidence."""


def execution_status(status, *, faults=(), cancellations=(), confirmed=None):
    if status == "failed" or faults:
        return "failed"
    if confirmed is False:
        return "unconfirmed"
    if status == "cancelled" or cancellations:
        return "cancelled"
    return status


def failure_evidence(category, detail):
    """Extract structured errors without inferring types from diagnostic text."""
    faults, cancellations = [], []
    for key in ("error", "recording_error", "backend_error", "diagnostic_error"):
        error = detail.get(key)
        error_type = error.get("type") if isinstance(error, dict) else None
        if key == "error":
            error_type = detail.get("error_type") or error_type
        if error_type:
            evidence = {"category": category, "phase": detail.get("phase"),
                        "error_type": error_type, "error": error}
            if error_type in ("KeyboardInterrupt", "builtins.KeyboardInterrupt"):
                cancellations.append(evidence)
                if category == "cleanup":
                    faults.append(evidence)
            else:
                faults.append(evidence)
    if not faults and not cancellations:
        faults.append({"category": category, "details": detail})
    return {"faults": faults, "cancellations": cancellations}
