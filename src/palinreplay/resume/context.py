"""Saved conversation views; no native Agent initialization or budget rewriting."""

import math
from dataclasses import dataclass

from palinreplay.agents.mini import MINI_COMMIT, MINI_VERSION
from palinreplay.core.errors import IntegrityError, RecordUsageError
from palinreplay.record.restore import RestoreMaterials


@dataclass(frozen=True)
class ResumePrefix:
    """Expose the saved objects and retain their original encoded payloads."""

    materials: RestoreMaterials

    @property
    def context(self):
        return self.materials.context

    @property
    def messages(self):
        return self.context["messages"]

    @property
    def config(self):
        return self.context["config"]

    @property
    def counters(self):
        return self.context["counters"]

    @property
    def input_position(self):
        return self.materials.input_position


def validate_resume_context(materials: RestoreMaterials) -> ResumePrefix:
    """Accept a saved mini decision boundary, independently of the parent's RunEnd."""
    if (materials.run.agent_name != "mini-swe-agent"
            or materials.run.agent_version != f"{MINI_VERSION}+{MINI_COMMIT}"):
        raise IntegrityError("Resume requires the pinned mini-swe-agent version")
    context, position = materials.context, materials.input_position
    messages = context.get("messages")
    if not isinstance(messages, list) or not messages or any(
        not isinstance(message, dict) or not isinstance(message.get("role"), str)
        for message in messages
    ):
        raise IntegrityError("unsupported Resume messages")
    for index, message in enumerate(messages):
        if message["role"] == "exit":
            extra = message.get("extra")
            reason = extra.get("exit_status", "exit") if isinstance(extra, dict) else "exit"
            raise RecordUsageError(
                f"C{materials.node.index} is terminal: exit message {index} ({reason})"
            )
    for key in ("config", "counters", "extra_template_vars", "template_vars", "clock",
                "global_stats", "model_accounting"):
        if not isinstance(context.get(key), dict):
            raise IntegrityError(f"missing saved mini context structure: {key}")
    for config in (materials.config, context["config"]):
        if not all(isinstance(config.get(key), dict) for key in ("agent", "model", "environment")):
            raise IntegrityError("missing saved mini configuration")
    counters = context["counters"]
    if (any(type(counters.get(key)) is not int or counters[key] < 0
            for key in ("n_calls", "consecutive_format_errors"))
            or type(counters.get("cost")) not in (int, float)
            or not math.isfinite(counters["cost"]) or counters["cost"] < 0):
        raise IntegrityError("invalid saved mini counters")
    if (type(position.get("next_message")) is not int
            or position["next_message"] != len(messages)
            or not isinstance(position.get("adoptions"), list)):
        raise IntegrityError("invalid Resume message cursor or adoptions")
    previous_stop, previous_event = None, 0
    for adoption in position["adoptions"]:
        if (not isinstance(adoption, dict)
                or any(type(adoption.get(key)) is not int
                       for key in ("start", "stop", "event_sequence"))
                or not 0 <= adoption["start"] < adoption["stop"] <= len(messages)
                or (previous_stop is not None and adoption["start"] != previous_stop)
                or not previous_event < adoption["event_sequence"] <= (
                    materials.node.accepted_event_sequence)
                or not isinstance(adoption.get("execution_id"), str)
                or not adoption["execution_id"]):
            raise IntegrityError("invalid Resume adoption boundary")
        previous_stop, previous_event = adoption["stop"], adoption["event_sequence"]
    if previous_stop is not None and previous_stop != len(messages):
        raise IntegrityError("Resume adoptions do not reach the message cursor")
    return ResumePrefix(materials)
