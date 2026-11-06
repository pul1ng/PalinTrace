"""Mini's message formatting/admission without its autonomous model loop."""

import logging
import time
from copy import deepcopy

from palinreplay.agents._mini_tools import pair_tool_messages
from palinreplay.agents.mini import load_native
from palinreplay.core.interfaces import AgentSnapshot, Payload


class ReplayContext:
    def __init__(self, materials):
        self.native = load_native()
        self.context = deepcopy(materials.context)
        self.messages = self.context["messages"]
        # Inherited adoption identities remain provenance, never local facts.
        self.inherited_position = deepcopy(materials.input_position)
        self.adoptions = []
        self.config = materials.config
        self.logger = logging.getLogger("agent")
        self.started = time.monotonic()
        self.supplied = 0
        self.base_calls = self.context.get("counters", {}).get("n_calls", 0)
        self.base_elapsed = self.context.get("clock", {}).get("elapsed_seconds", 0)

    def historical_answer(self, history):
        self.supplied += 1
        counters = self.context.setdefault("counters", {})
        counters["n_calls"] = self.base_calls + self.supplied
        format_error = history.model.details.get("backend", {}).get("parse") == "format-error"
        counters["consecutive_format_errors"] = (
            counters.get("consecutive_format_errors", 0) + 1 if format_error else 0
        )
        variables = self.context.setdefault("template_vars", {})
        variables["n_model_calls"] = counters["n_calls"]
        variables["model_cost"] = counters.get("cost", 0)
        variables["elapsed_seconds"] = self.base_elapsed + int(time.monotonic() - self.started)

    def adopt(self, deliveries, messages):
        return self.native.adopt_messages(
            self.messages, self.adoptions, deliveries, messages,
            lambda offered: self.native.DefaultAgent.add_messages(self, *messages),
        )

    def observations(self, history, results):
        from minisweagent.models.utils.actions_toolcall import format_toolcall_observation_messages

        if len(results) != len(history.tools):
            raise ValueError("incomplete tool group cannot produce mini observations")
        actions = [tool.request["action"] for tool in history.tools]
        model = self.config["model"]
        return format_toolcall_observation_messages(
            actions=actions, outputs=[self.native.BorrowedEnvironment.output(r) for r in results],
            observation_template=model["observation_template"],
            template_vars=deepcopy(self.context.get("template_vars", {})),
            multimodal_regex=model.get("multimodal_regex", ""),
        )

    def accept_results(self, history, calls, results):
        if not calls and not results and not history.tools:
            return []
        messages = self.observations(history, results)
        deliveries = pair_tool_messages(
            calls, messages, error_message="mini tool observations do not align with recorded calls",
        )
        return self.adopt(deliveries, messages)

    def export(self, boundary):
        context = deepcopy(self.context)
        context["messages"] = deepcopy(self.messages)
        context["replay"] = {"historical_input_position": self.inherited_position,
                             "historical_answer_supplies": self.supplied,
                             "actual_model_calls": 0, "actual_model_cost": 0}
        return AgentSnapshot(Payload.json(context), Payload.json({
            "next_message": len(self.messages), "adoptions": self.adoptions,
        }))
