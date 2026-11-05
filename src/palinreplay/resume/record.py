"""Record one tool group from an already obtained Resume decision."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import fields

from palinreplay.core.errors import RecordUsageError
from palinreplay.resume.decision import ResumeDecision
from palinreplay.resume.report import exception_evidence
from palinreplay.storage.io import write_json


@contextmanager
def _claim_decision(session, decision):
    """Keep the session open and consume its saved decision before any execution."""
    if not session._decision_lock.acquire(blocking=False):
        raise RecordUsageError("Resume decision is already being consumed")
    try:
        if session.report["phase"] != "caller" or session.executor.state != "running":
            raise RecordUsageError("Resume recording requires an active session")
        if session._record_attempt:
            raise RecordUsageError("Resume decision was already consumed")
        saved = session._decision_evidence
        if saved.get("status") != "succeeded":
            raise RecordUsageError("Resume recording requires a successful saved decision")
        node = session.start.node
        source = {"store": str(session.start.source_store), "run_id": node.run_id,
                  "node_id": node.node_id, "index": node.index, "state_id": node.state_id}
        if (not isinstance(decision, ResumeDecision) or saved["source"] != source
                or any(getattr(decision, item.name) != saved[item.name]
                       for item in fields(ResumeDecision))):
            raise RecordUsageError("Resume decision differs from the saved fourth-step evidence")
        attempt = session._record_attempt
        attempt.update(status="consuming", source=deepcopy(source), model_requests=0,
                       tool_executions=0, complete_nodes=0)
        session.report["recording"] = attempt
        try:
            write_json(session.report_path, session.report, replace=True)
        except BaseException as error:
            attempt.update(status="failed", error=exception_evidence(error))
            raise
        yield deepcopy(saved)
    finally:
        session._decision_lock.release()
