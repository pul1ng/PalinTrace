"""Prepare the initial Resume decision and record later branch model calls."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field

from pydantic import ValidationError

from palinreplay.agents.mini import (
    complete_decision_request,
    decision_cost,
    load_native,
    parse_decision_actions,
    prepare_decision_request,
)
from palinreplay.core.errors import RecordUsageError
from palinreplay.core.interfaces import CallAdapter, Payload, ResultCapture
from palinreplay.core.progress import ProgressEvent
from palinreplay.llm import LLMConfig, mini_model_name
from palinreplay.resume.report import exception_evidence
from palinreplay.storage.io import write_json


@dataclass
class ResumeDecision:
    """Actual model output; messages and tools have not been executed or adopted."""

    messages: list[dict]
    assistant_message: dict
    raw_response: dict
    actions: list[dict]
    accounting: dict


class ResumeDecisionError(RuntimeError):
    """A classified failure with the actual decision evidence obtained so far."""

    def __init__(self, category, decision):
        self.category, self.decision = category, deepcopy(decision)
        super().__init__(f"Resume decision {category} failed: {decision['error']['error']}")


def _failed_decision(session, category, error):
    evidence = session.report["decision"]
    details = {"category": category, **exception_evidence(error)}
    if getattr(error, "status_code", None) is not None:
        details["status_code"] = error.status_code
    interrupted = not isinstance(error, Exception)
    evidence.update(status="interrupted" if interrupted else "failed", error=details,
                    decisions=0, actions=[])
    failure = error if interrupted else ResumeDecisionError(category, evidence)
    try:
        write_json(session.report_path, session.report, replace=True)
    except BaseException as write_error:
        failure.add_note(f"Decision evidence write also failed: {type(write_error).__name__}")
    return failure


def decide_resume_start(session, *, instruction=None, llm_config=None, defer_format_errors=False):
    """Ask once; the CLI defers native format feedback to run_resume_steps()."""
    with _first_attempt(session, instruction, llm_config) as prepared:
        return _request_decision(session, prepared, defer_format_errors=defer_format_errors)


def _request_decision(session, prepared, *, defer_format_errors=False):
    evidence = session.report["decision"]
    dispatched = False
    category = "evidence"
    try:
        evidence.update(status="requesting", model_requests=1,
                        remaining_steps=session.start.max_steps - 1)
        write_json(session.report_path, session.report, replace=True)
        category = "request"
        session.progress(ProgressEvent("model", step=session.start.node.index + 1, request=1))
        dispatched = True
        response = complete_decision_request(prepared.request, prepared.credentials)
        category = "tool_format"
        raw = response.model_dump(mode="json")
        evidence.update(status="returned", raw_response=raw, usage=deepcopy(raw.get("usage")))
        assistant = deepcopy(raw["choices"][0]["message"])
        evidence.update(assistant_message=assistant, **decision_cost(prepared.native_model, response))
        category = "evidence"
        write_json(session.report_path, session.report, replace=True)
        session.progress(ProgressEvent("model_returned", step=session.start.node.index + 1, request=1))
        category = "tool_format"
        try:
            actions = parse_decision_actions(prepared.native_model, response)
        except load_native().FormatError as error:
            if not defer_format_errors:
                raise
            actions = []
            evidence.update(status="format_error", decisions=0,
                            format_error={"messages": deepcopy(error.messages)})
        else:
            evidence.update(status="succeeded", decisions=1)
        category = "evidence"
        evidence["actions"] = deepcopy(actions)
        write_json(session.report_path, session.report, replace=True)
    except BaseException as error:
        if not dispatched:
            evidence.update(model_requests=0, remaining_steps=session.start.max_steps)
        raise _failed_decision(session, category, error) from None
    accounting = {key: deepcopy(evidence[key]) for key in (
        "inherited_counters", "max_steps", "model_requests", "decisions", "remaining_steps",
        "usage", "cost", "cost_known",
    )}
    session._decision_evidence.update(deepcopy(evidence))
    session._decision_evidence["accounting"] = deepcopy(accounting)
    return ResumeDecision(prepared.messages, deepcopy(assistant), deepcopy(raw), actions, accounting)


@dataclass
class PreparedResumeDecision:
    messages: list[dict]
    request: dict
    credentials: LLMConfig = field(repr=False)
    native_model: object = field(repr=False)


def prepare_branch_decision(session, messages, *, llm_config=None):
    """Prepare mini input from the complete conversation supplied by the branch."""
    messages = deepcopy(messages)
    credentials = resolve_resume_model(session, llm_config=llm_config)
    config = deepcopy(session.prefix.config["model"])
    config.update(model_name=credentials.model, cost_tracking="ignore_errors")
    options = config.get("model_kwargs", {})
    for location, values in (("model_kwargs", options),
                             ("model_kwargs.extra_body", options.get("extra_body") or {})):
        for key in ("model", "messages", "tools"):
            if key in values:
                raise ValueError(f"{location}.{key} cannot override the Resume request")
    config["model_kwargs"] = options | {
        "timeout": credentials.timeout, "num_retries": 0, "max_retries": 0,
    }
    model, request = prepare_decision_request(messages, config)
    return PreparedResumeDecision(messages, request, credentials, model)


def request_branch_response(session, step, context, *, llm_config=None):
    """Persist one actual response before the native Agent parses or adopts it."""
    prepared = prepare_branch_decision(session, context.messages, llm_config=llm_config)
    accounting = {}

    def execute(request):
        session.progress(ProgressEvent("model", step=step.step.index,
                                       request=session._record_attempt["model_requests"] + 2))
        session._record_attempt["model_requests"] += 1
        accounting.update(model_requests=1, usage=None, cost=None, cost_known=False,
                          execution_id=call.execution_id, step_id=step.step.step_id)
        session._record_attempt["request_accounting"].append(accounting)
        response = complete_decision_request(request, prepared.credentials)
        raw = response.model_dump(mode="json")
        call.observe("response", Payload.json(raw))
        accounting.update(model_requests=1, usage=deepcopy(raw.get("usage")),
                          **decision_cost(prepared.native_model, response))
        return response

    adapter = CallAdapter(
        name="resume.model", execute=execute, capture_request=Payload.json,
        capture_context=lambda request: Payload.json(prepared.messages),
        capture_result=lambda response: ResultCapture(
            Payload.json(response.model_dump(mode="json")), "litellm-sdk-response-json",
            details={"accounting": deepcopy(accounting), "actual_model_calls": 1},
            limitations=("SDK representation, not HTTP wire bytes.",),
        ),
        visibility="observed-attempt",
    )
    call = step.model(adapter, prepared.request)
    response = call.invoke()
    context.account_decision(accounting)
    session.progress(ProgressEvent("model_returned", step=step.step.index,
                                   request=session._record_attempt["model_requests"] + 1))
    return prepared, call, response, accounting


def record_branch_decision(session, step, context, *, llm_config=None):
    """Request and adopt one decision without running the native continuation loop."""
    prepared, call, response, accounting = request_branch_response(
        session, step, context, llm_config=llm_config,
    )
    raw = response.model_dump(mode="json")
    assistant = deepcopy(raw["choices"][0]["message"])
    actions = parse_decision_actions(prepared.native_model, response)
    session._record_attempt["decisions"] += 1
    context.accept_response(step, call, assistant, actions)
    return ResumeDecision(prepared.messages, assistant, raw, actions, accounting)


def prepare_resume_decision(session, *, instruction=None, llm_config=None):
    """Persist the exact public model input before it can be sent."""
    with _first_attempt(session, instruction, llm_config) as prepared:
        return prepared


@contextmanager
def _first_attempt(session, instruction, llm_config):
    already_attempted = "The first decision for this Resume session was already attempted"
    if not session._decision_lock.acquire(blocking=False):
        raise RecordUsageError(already_attempted)
    try:
        if session.report["phase"] != "caller" or session.executor.state != "running":
            raise RecordUsageError("Resume decisions require an active session")
        if "decision" in session.report:
            raise RecordUsageError(already_attempted)
        yield _prepare_decision(session, instruction, llm_config)
    finally:
        session._decision_lock.release()


def _prepare_decision(session, instruction, llm_config):
    node = session.start.node
    evidence = session.report["decision"] = {
        "status": "configuring",
        "source": {"store": str(session.start.source_store), "run_id": node.run_id,
                   "node_id": node.node_id, "index": node.index, "state_id": node.state_id},
        "model": session.start.model, "instruction": instruction,
        "inherited_message_count": len(session.prefix.messages),
        "request_representation": "public litellm.completion arguments",
        "model_requests": 0, "tool_executions": 0, "task_file_updates": 0,
        "file_snapshots": 0, "complete_nodes": 0,
        "inherited_counters": deepcopy(session.prefix.counters),
        "max_steps": session.start.max_steps, "decisions": 0,
        "remaining_steps": session.start.max_steps, "usage": None,
        "cost": None, "cost_known": False, "actions": [],
    }
    category = "config"
    try:
        messages = build_resume_messages(session, instruction=instruction)
        evidence["messages"] = deepcopy(messages)
        prepared = prepare_branch_decision(session, messages, llm_config=llm_config)
        category = "evidence"
        evidence.update(status="prepared", model=prepared.credentials.model,
                        request=deepcopy(prepared.request))
        write_json(session.report_path, session.report, replace=True)
    except BaseException as error:
        raise _failed_decision(session, category, error) from None
    return prepared


def resolve_resume_model(session, *, llm_config=None):
    """Load private connection settings while selecting this branch's model."""
    try:
        config = LLMConfig.load(llm_config, model=session.start.model, max_retries=0)
    except ValidationError as error:
        fields = ", ".join(".".join(map(str, item["loc"])) for item in error.errors())
        raise ValueError(f"Resume model configuration invalid: {fields}") from None
    except (OSError, ValueError) as error:
        raise ValueError(
            f"Resume model configuration could not be loaded: {type(error).__name__}"
        ) from None
    return config.model_copy(update={"model": mini_model_name(config.model)})


def build_resume_messages(session, *, instruction=None):
    """Copy the saved prefix and append one optional branch-owned user message."""
    if instruction is not None and (
        not isinstance(instruction, str) or not instruction.strip()
    ):
        raise ValueError("instruction must be nonblank text")
    messages = deepcopy(session.prefix.messages)
    if instruction is not None:
        messages.append({"role": "user", "content": instruction})
    return messages
