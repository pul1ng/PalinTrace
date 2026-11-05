"""One model decision from a restored boundary, without execution or node commits."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field

from pydantic import ValidationError

from palinreplay.agents.mini import (
    complete_decision_request,
    decision_cost,
    parse_decision_actions,
    prepare_decision_request,
)
from palinreplay.core.errors import RecordUsageError
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


def decide_resume_start(session, *, instruction=None, llm_config=None):
    """Ask the selected model once from a service-ready Resume boundary."""
    with _first_attempt(session, instruction, llm_config) as prepared:
        return _request_decision(session, prepared)


def _request_decision(session, prepared):
    evidence = session.report["decision"]
    dispatched = False
    category = "evidence"
    try:
        evidence.update(status="requesting", model_requests=1,
                        remaining_steps=session.start.max_steps - 1)
        write_json(session.report_path, session.report, replace=True)
        category = "request"
        dispatched = True
        response = complete_decision_request(prepared.request, prepared.credentials)
        category = "tool_format"
        raw = response.model_dump(mode="json")
        evidence.update(status="returned", raw_response=raw, usage=deepcopy(raw.get("usage")))
        assistant = deepcopy(raw["choices"][0]["message"])
        evidence.update(assistant_message=assistant, **decision_cost(prepared.native_model, response))
        category = "evidence"
        write_json(session.report_path, session.report, replace=True)
        category = "tool_format"
        actions = parse_decision_actions(prepared.native_model, response)
        category = "evidence"
        evidence.update(status="succeeded", decisions=1, actions=deepcopy(actions))
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
        category = "evidence"
        evidence.update(status="prepared", model=credentials.model, request=deepcopy(request))
        write_json(session.report_path, session.report, replace=True)
    except BaseException as error:
        raise _failed_decision(session, category, error) from None
    return PreparedResumeDecision(messages, request, credentials, model)


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
