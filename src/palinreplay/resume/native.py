"""Resume recording hooks around Record's pinned DefaultAgent control loop."""

import time
from copy import deepcopy

from palinreplay.agents.mini import load_native, parse_decision_actions
from palinreplay.core.errors import IncompleteRecordError
from palinreplay.resume.decision import prepare_branch_decision, request_branch_response
from palinreplay.resume.environment import saved_execution_config
from palinreplay.resume.record import _execute_tool
from palinreplay.resume.recording import begin_recording_step

native = load_native()


class ResumeModel:
    """Keep actual call evidence while native query/run own message admission."""

    def __init__(self, session, context, evidence, llm_config):
        self.session, self.context, self.evidence = session, context, evidence
        self.llm_config = llm_config
        self.native_model = prepare_branch_decision(
            session, evidence["messages"], llm_config=llm_config,
        ).native_model
        self.first = True
        self.format_error = None

    def query(self, messages):
        agent, attempt = self.agent, self.session._record_attempt
        if self.first:
            self.first = False
            call = self.context.saved_decision_call(agent.active, self.evidence)
            raw, accounting = self.evidence["raw_response"], self.evidence["accounting"]
            assistant, actions = self.evidence["assistant_message"], self.evidence["actions"]
            pending_error = self.evidence.get("format_error")
        else:
            attempt["stage"] = "begin_step"
            agent.active = begin_recording_step(self.session, agent.recorder)
            attempt["stage"] = "model"
            prepared, call, response, accounting = request_branch_response(
                self.session, agent.active, self.context, llm_config=self.llm_config,
            )
            self.native_model = prepared.native_model
            raw = response.model_dump(mode="json")
            assistant = raw["choices"][0]["message"]
            try:
                actions = parse_decision_actions(self.native_model, response)
            except native.FormatError as error:
                actions, pending_error = [], {"messages": error.messages}
            else:
                pending_error = None
                attempt["decisions"] += 1
        agent.model_call, agent.actions = call, actions
        agent.model_pending = True
        if pending_error is not None:
            error = native.FormatError(*deepcopy(pending_error["messages"]))
            error.messages[0].setdefault("extra", {}).update(
                cost=accounting["cost"] or 0, response=deepcopy(raw),
            )
            error.args = (error.messages[0]["content"],)
            self.format_error = error
            attempt["format_errors"] = attempt.get("format_errors", 0) + 1
            raise error
        message = deepcopy(assistant)
        message.setdefault("extra", {}).update(
            actions=deepcopy(actions), response=deepcopy(raw), cost=accounting["cost"] or 0,
        )
        return message

    def format_observation_messages(self, *args):
        return self.native_model.format_observation_messages(*args)

    def format_message(self, **kwargs):
        return self.native_model.format_message(**kwargs)

    def get_template_vars(self):
        return self.native_model.get_template_vars()


class ResumeEnvironment:
    """Borrow the restored executor; DefaultAgent executes the ordered tool group."""

    def __init__(self, session):
        self.session = session

    def execute(self, action):
        agent = self.agent
        self.session._record_attempt["stage"] = "tools"
        call, result = _execute_tool(self.session, agent.active, action)
        agent.tool_calls.append(call)
        output = native.BorrowedEnvironment.output(result)
        try:
            native.DockerEnvironment._check_finished(self, output)
        except native.Submitted:
            agent.submission_call = call
            raise
        return output

    def get_template_vars(self):
        return saved_execution_config(self.session.materials)


class ResumeAgent(native.DefaultAgent):
    """Inherit run/query/execute_actions, including native error and limit handling.

    The initial add_messages hook substitutes the restored prefix for run()'s
    fresh-task messages. Later hooks only record native admissions and boundaries.
    """

    def __init__(self, session, context, evidence, recorder, step, *, llm_config=None):
        model = ResumeModel(session, context, evidence, llm_config)
        env = ResumeEnvironment(session)
        inherited = session.prefix.counters
        config = {"system_template": "", "instance_template": "",
                  **deepcopy(context.context["config"]["agent"])}
        # Resume has its own new-decision budget, independent of the old run's
        # limits and output path. The cumulative counters still retain history.
        config.update(step_limit=inherited["n_calls"] + session.start.max_steps,
                      cost_limit=0, wall_time_limit_seconds=0, output_path=None)
        super().__init__(model, env, **config)
        model.agent = env.agent = self
        self.session, self.context, self.recorder, self.active = session, context, recorder, step
        self.n_calls, self.cost = inherited["n_calls"], inherited.get("cost", 0)
        self.n_consecutive_format_errors = inherited.get("consecutive_format_errors", 0)
        self.extra_template_vars = deepcopy(context.context.get("extra_template_vars", {}))
        self.seeding = True
        self.model_pending = False
        self.model_call = None
        self.actions, self.tool_calls, self.nodes = [], [], []
        self.submission_call = None
        self.step_error = None
        self.boundary_pending = False

    def _render_template(self, template):
        # Resume already has the original system/task messages and optional guide.
        return "" if self.seeding else super()._render_template(template)

    def step(self):
        self.step_error = None
        self.tool_calls, self.submission_call = [], None
        self.session._record_attempt["stage"] = "model"
        try:
            return super().step()
        except BaseException as error:
            self.step_error = error
            raise
        finally:
            self.boundary_pending = True

    def add_messages(self, *messages):
        if self.seeding:
            self.seeding = False
            self.messages = self.context.messages
            return list(self.messages)
        if self.model_pending:
            self.model_pending = False
            result = self.context.adopt([(self.model_call, list(messages))], list(messages))
            self.active.declare_tools(
                self.model_call, tuple(action["tool_call_id"] for action in self.actions),
            )
            return result
        if self.submission_call is not None and isinstance(self.step_error, native.Submitted):
            return self.context.adopt([(self.submission_call, list(messages))], list(messages))
        if self.tool_calls and self.step_error is None:
            self.session._record_attempt["stage"] = "admission"
            if len(messages) != len(self.tool_calls) or any(
                message.get("tool_call_id") != call.operation.tool_call_id
                for message, call in zip(messages, self.tool_calls, strict=False)
            ):
                raise ValueError("mini observations do not align with Resume tool calls")
            return self.context.adopt(
                [(call, [message]) for call, message in zip(self.tool_calls, messages, strict=True)],
                list(messages),
            )
        return super().add_messages(*messages)

    def _sync_context(self):
        self.context.context.update(
            messages=self.messages, extra_template_vars=deepcopy(self.extra_template_vars),
            counters={"n_calls": self.n_calls, "cost": self.cost,
                      "consecutive_format_errors": self.n_consecutive_format_errors},
            template_vars=self.get_template_vars(),
            clock={"started_at": self._start_time, "boundary_at": time.time(),
                   "elapsed_seconds": int(time.time() - self._start_time)},
        )
        self.context.context["config"]["agent"] = self.config.model_dump(mode="json")

    def save(self, path, *extra_dicts):
        # The native run calls save after adopting FormatError/Submitted messages.
        self._sync_context()
        pending, self.boundary_pending = self.boundary_pending, False
        if not pending or self.recorder.phase != "step":
            return self.context.context
        if self.step_error is not None and not isinstance(
            self.step_error, (native.FormatError, native.Submitted)
        ):
            return self.context.context
        try:
            self.recorder.store.accepted_step(self.active.step.step_id)
        except IncompleteRecordError:
            if isinstance(self.step_error, native.Submitted):
                return self.context.context
            raise
        attempt = self.session._record_attempt
        attempt["stage"] = "commit"
        node = self.recorder.commit_step()
        self.nodes.append(node)
        attempt.update(stage="step_committed", complete_nodes=len(self.nodes), node_id=node.node_id,
                       state_id=node.state_id, message_count=len(self.messages))
        return self.context.context

    def end_details(self):
        return {
            "agent_exit_status": self.messages[-1].get("extra", {}).get("exit_status"),
            "record_complete": self.recorder.phase == "ready",
            "final_control_context": deepcopy(self.context.context),
            "final_input_position": deepcopy(self.context.input_position),
        }
