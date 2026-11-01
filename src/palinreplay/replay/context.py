"""Mini's message formatting/admission without its autonomous model loop."""

import logging
from copy import deepcopy

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
        if len(messages) != len(calls) or any(
            msg.get("tool_call_id") != call.operation.tool_call_id
            for msg, call in zip(messages, calls, strict=True)
        ):
            raise ValueError("mini tool observations do not align with recorded calls")
        return self.adopt([(call, [msg]) for call, msg in zip(calls, messages, strict=True)], messages)

    def export(self, boundary):
        context = deepcopy(self.context)
        context["messages"] = deepcopy(self.messages)
        context["replay"] = {"historical_input_position": self.inherited_position}
        return AgentSnapshot(Payload.json(context), Payload.json({
            "next_message": len(self.messages), "adoptions": self.adoptions,
        }))
