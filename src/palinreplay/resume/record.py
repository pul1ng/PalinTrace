"""Record one tool group from an already obtained Resume decision."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import fields

from palinreplay.agents.mini import load_native
from palinreplay.core.errors import RecordUsageError
from palinreplay.core.interfaces import CallAdapter, Payload, ResultCapture
from palinreplay.core.models import Node
from palinreplay.resume.decision import ResumeDecision
from palinreplay.resume.environment import saved_execution_config
from palinreplay.resume.record_context import ResumeRecordContext
from palinreplay.resume.recording import finish_recording, open_recording
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


def record_resume_step(session, decision) -> Node | None:
    """Return the committed node, or None if submission left the tool group incomplete."""
    with _claim_decision(session, decision) as evidence:
        try:
            context = ResumeRecordContext(session, evidence)
            with open_recording(session, context.export) as (recorder, step):
                context.accept_decision(step, evidence)
                node, details = _execute_and_save_group(
                    session, recorder, step, context, evidence["actions"],
                )
                if node is not None:
                    recorder.finish("Saved Resume decision completed", details=details)
                else:
                    recorder.end_incomplete("Native Agent submitted with an incomplete tool group",
                                            details=details)
        except BaseException as error:
            finish_recording(session, error)
            raise
        finish_recording(session)
        return node


def _execute_and_save_group(session, recorder, step, context, actions):
    """Execute and commit one group; the caller owns the recording's RunEnd."""
    calls, results, submission = _execute_tools(session, step, actions)
    complete = context.accept_results(actions, calls, results, submission=submission)
    details = None
    if submission is not None:
        details = {
            "agent_exit_status": context.messages[-1]["extra"]["exit_status"],
            "record_complete": complete,
            "final_control_context": context.context,
            "final_input_position": context.input_position,
        }
    node = recorder.commit_step() if complete else None
    if node is not None:
        attempt = session._record_attempt
        attempt.update(complete_nodes=attempt["complete_nodes"] + 1,
                       node_id=node.node_id, state_id=node.state_id,
                       message_count=len(context.messages))
    return node, details


def _execute_tools(session, step, actions):
    """Persist each actual return before entering the next tool in the saved order."""
    config = saved_execution_config(session.materials)
    native = load_native()
    calls, results = [], []

    def execute(request):
        session._record_attempt["tool_executions"] += 1
        return session.executor.execute(request)

    adapter = CallAdapter(
        name="resume.bash", execute=execute, capture_request=Payload.json,
        capture_result=lambda result: ResultCapture(
            Payload(result.output), "sandbox-merged-output-bytes",
            details={"decoded": native.BorrowedEnvironment.output(result),
                     "duration_sec": result.duration_sec},
            outcome="error" if result.exit_code else "returned",
        ),
        visibility="observed-attempt",
    )
    for action in actions:
        request = {"action": deepcopy(action), "cwd": config["cwd"], "env": config["env"],
                   "interpreter": config["interpreter"], "timeout_sec": config["timeout"]}
        call = step.tool(adapter, request, tool_call_id=action["tool_call_id"])
        calls.append(call)
        results.append(call.invoke())
        session._record_attempt["tool_results"] = len(results)
        write_json(session.report_path, session.report, replace=True)
        try:
            native.DockerEnvironment._check_finished(
                session.executor, native.BorrowedEnvironment.output(results[-1]),
            )
        except native.Submitted as submitted:
            return calls, results, submitted
    return calls, results, None
