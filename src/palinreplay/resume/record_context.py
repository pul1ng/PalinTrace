"""Mini message admission for one saved Resume decision, without an Agent loop."""

import logging
from copy import deepcopy

from palinreplay.agents.mini import load_native
from palinreplay.core.interfaces import AgentSnapshot, CallAdapter, Payload, ResultCapture


class ResumeRecordContext:
    def __init__(self, session, evidence):
        self.native = load_native()
        self.context = deepcopy(session.prefix.context)
        self.messages = deepcopy(evidence["messages"])
        self.context["messages"] = self.messages
        self.inherited_position = deepcopy(session.input_position)
        self.adoptions = []
        self.logger = logging.getLogger("agent")
        self.context["config"]["model"]["model_name"] = evidence["model"]
        accounting = evidence["accounting"]
        counters = self.context["counters"]
        counters["n_calls"] += accounting["model_requests"]
        counters["consecutive_format_errors"] = 0
        counters["cost"] = counters.get("cost", 0) + (accounting["cost"] or 0)
        self.context.setdefault("template_vars", {}).update(
            n_model_calls=counters["n_calls"], model_cost=counters["cost"],
        )
        self.context["resume"] = {
            "inherited_input_position": self.inherited_position,
            "decision_accounting": deepcopy(accounting), "recording_model_requests": 0,
        }
        model = self.context.setdefault("model_accounting", {})
        model.setdefault("calls", []).append({"known": accounting["cost_known"],
                                              "cost": accounting["cost"]})
        model["client_requests"] = model.get("client_requests", 0) + accounting["model_requests"]
        model["cost_known"] = model.get("cost_known", False) and accounting["cost_known"]

    def adopt(self, deliveries, messages):
        return self.native.adopt_messages(
            self.messages, self.adoptions, deliveries, messages,
            lambda offered: self.native.DefaultAgent.add_messages(self, *messages),
        )

    def accept_decision(self, step, evidence):
        """Supply the fourth-step response locally, then seal its whole tool group."""
        adapter = CallAdapter(
            name="resume.saved-decision",
            execute=lambda request: deepcopy(evidence["raw_response"]),
            capture_request=Payload.json,
            capture_context=lambda request: Payload.json(evidence["messages"]),
            capture_result=lambda raw: ResultCapture(
                Payload.json(raw), "fourth-step-model-response",
                details={"accounting": deepcopy(evidence["accounting"]),
                         "actual_model_calls": 0, "source": deepcopy(evidence["source"])},
            ),
            visibility="observed-attempt",
        )
        call = step.model(adapter, deepcopy(evidence["request"]))
        call.invoke()
        assistant = deepcopy(evidence["assistant_message"])
        assistant.setdefault("extra", {}).update(actions=deepcopy(evidence["actions"]))
        self.adopt([(call, [assistant])], [assistant])
        step.declare_tools(call, tuple(action["tool_call_id"] for action in evidence["actions"]))
        return call

    def accept_results(self, actions, calls, results):
        """Admit a complete group using mini's own observation representation."""
        from minisweagent.models.utils.actions_toolcall import format_toolcall_observation_messages

        if len(results) != len(actions) or len(calls) != len(actions):
            raise ValueError("incomplete Resume tool group cannot be adopted")
        outputs = [self.native.BorrowedEnvironment.output(result) for result in results]
        messages = None
        for output in outputs:
            try:
                self.native.DockerEnvironment._check_finished(None, output)
            except self.native.Submitted as submitted:
                if len(actions) != 1:
                    raise ValueError("submission short-circuited an incomplete tool group") from submitted
                messages = submitted.messages
        if messages is not None:
            return self.adopt([(calls[0], messages)], messages)
        config = self.context["config"]["model"]
        messages = format_toolcall_observation_messages(
            actions=actions, outputs=outputs, observation_template=config["observation_template"],
            template_vars=deepcopy(self.context.get("template_vars", {})),
            multimodal_regex=config.get("multimodal_regex", ""),
        )
        if len(messages) != len(calls) or any(
            message.get("tool_call_id") != call.operation.tool_call_id
            for message, call in zip(messages, calls, strict=True)
        ):
            raise ValueError("mini observations do not align with Resume tool calls")
        return self.adopt([(call, [message]) for call, message in zip(calls, messages, strict=True)],
                          messages)

    def export(self, boundary):
        return AgentSnapshot(Payload.json(self.context), Payload.json({
            "next_message": len(self.messages), "adoptions": self.adoptions,
        }))
