"""Mini message admission and accounting for a growing Resume conversation."""

import logging
from copy import deepcopy

from palinreplay.agents._mini_tools import pair_tool_messages
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
        self.context["resume"] = {
            "inherited_input_position": self.inherited_position,
            "first_decision_accounting": deepcopy(evidence["accounting"]),
            "recording_model_requests": 0,
        }
        self.account_decision(evidence["accounting"], recording=False)

    def account_decision(self, accounting, *, recording=True):
        """Extend inherited counters once for each actual branch model request."""
        counters = self.context["counters"]
        counters["n_calls"] += accounting["model_requests"]
        counters["consecutive_format_errors"] = 0
        counters["cost"] = counters.get("cost", 0) + (accounting["cost"] or 0)
        self.context.setdefault("template_vars", {}).update(
            model_name=self.context["config"]["model"]["model_name"],
            n_model_calls=counters["n_calls"], model_cost=counters["cost"],
        )
        resume = self.context["resume"]
        if recording:
            resume["recording_model_requests"] += accounting["model_requests"]
        resume["remaining_steps"] = (
            resume["first_decision_accounting"]["remaining_steps"] - resume["recording_model_requests"]
        )
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

    def saved_decision_call(self, step, evidence):
        """Supply the already requested response without invoking the model again."""
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
        return call

    def accept_decision(self, step, evidence):
        """Supply the fourth-step response locally, then seal its whole tool group."""
        call = self.saved_decision_call(step, evidence)
        self.accept_response(step, call, evidence["assistant_message"], evidence["actions"])
        return call

    def accept_response(self, step, call, assistant_message, actions):
        """Adopt this round's answer into the same growing branch conversation."""
        assistant = deepcopy(assistant_message)
        assistant.setdefault("extra", {}).update(actions=deepcopy(actions))
        self.adopt([(call, [assistant])], [assistant])
        step.declare_tools(call, tuple(action["tool_call_id"] for action in actions))

    def accept_results(self, actions, calls, results, *, submission=None) -> bool:
        """Admit what mini received and report whether the whole declared group was adopted."""
        from minisweagent.models.utils.actions_toolcall import format_toolcall_observation_messages

        if submission is not None:
            # Native execution stops before the ordinary tool observations are delivered.
            self.adopt([(calls[-1], submission.messages)], submission.messages)
            return len(actions) == 1
        if len(results) != len(actions) or len(calls) != len(actions):
            raise ValueError("incomplete Resume tool group cannot be adopted")
        outputs = [self.native.BorrowedEnvironment.output(result) for result in results]
        config = self.context["config"]["model"]
        messages = format_toolcall_observation_messages(
            actions=actions, outputs=outputs, observation_template=config["observation_template"],
            template_vars=deepcopy(self.context.get("template_vars", {})),
            multimodal_regex=config.get("multimodal_regex", ""),
        )
        deliveries = pair_tool_messages(
            calls, messages, error_message="mini observations do not align with Resume tool calls",
        )
        self.adopt(deliveries, messages)
        return True

    @property
    def input_position(self):
        return {"next_message": len(self.messages), "adoptions": self.adoptions}

    def export(self, boundary):
        return AgentSnapshot(Payload.json(self.context), Payload.json(self.input_position))
