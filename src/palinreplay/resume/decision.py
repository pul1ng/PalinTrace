"""Branch-owned input for decisions at a restored Resume boundary."""

from copy import deepcopy
from dataclasses import dataclass, field

from pydantic import ValidationError

from palinreplay.agents.mini import complete_decision_request, prepare_decision_request
from palinreplay.llm import LLMConfig, mini_model_name
from palinreplay.storage.io import write_json


@dataclass
class ResumeDecision:
    """Actual model output; messages and tools have not been executed or adopted."""

    messages: list[dict]
    assistant_message: dict
    raw_response: dict


def decide_resume_start(session, *, instruction=None, llm_config=None):
    """Ask the selected model once from a service-ready Resume boundary."""
    prepared = prepare_resume_decision(session, instruction=instruction, llm_config=llm_config)
    evidence = session.report["decision"]
    evidence.update(status="requesting", model_requests=1)
    response = complete_decision_request(prepared.request, prepared.credentials)
    raw = response.model_dump(mode="json")
    assistant = deepcopy(raw["choices"][0]["message"])
    evidence.update(status="returned", raw_response=raw, assistant_message=assistant)
    write_json(session.report_path, session.report, replace=True)
    return ResumeDecision(prepared.messages, assistant, raw)


@dataclass
class PreparedResumeDecision:
    messages: list[dict]
    request: dict
    credentials: LLMConfig = field(repr=False)
    native_model: object = field(repr=False)


def prepare_resume_decision(session, *, instruction=None, llm_config=None):
    """Persist the exact public model input before it can be sent."""
    messages = build_resume_messages(session, instruction=instruction)
    credentials = resolve_resume_model(session, llm_config=llm_config)
    config = deepcopy(session.prefix.config["model"])
    config.update(model_name=credentials.model, cost_tracking="ignore_errors")
    config["model_kwargs"] = config.get("model_kwargs", {}) | {
        "timeout": credentials.timeout, "num_retries": 0, "max_retries": 0,
    }
    model, request = prepare_decision_request(messages, config)
    node = session.start.node
    session.report["decision"] = {
        "status": "prepared",
        "source": {"store": str(session.start.source_store), "run_id": node.run_id,
                   "node_id": node.node_id, "index": node.index, "state_id": node.state_id},
        "model": credentials.model, "instruction": instruction,
        "inherited_message_count": len(session.prefix.messages),
        "messages": deepcopy(messages), "request": deepcopy(request),
        "request_representation": "public litellm.completion arguments",
        "model_requests": 0, "tool_executions": 0, "task_file_updates": 0,
        "file_snapshots": 0, "complete_nodes": 0,
    }
    write_json(session.report_path, session.report, replace=True)
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
