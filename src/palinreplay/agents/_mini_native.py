"""Hooks for one verified upstream commit. Import through agents.mini only."""

from __future__ import annotations

import json
import os
import platform
import time
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass
from types import FunctionType, MethodType
from typing import ClassVar

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.docker import DockerEnvironment, DockerEnvironmentConfig
from minisweagent.exceptions import FormatError, Submitted
from minisweagent.models import GlobalModelStats
from minisweagent.models.litellm_model import LitellmModel
from minisweagent.models.utils.actions_toolcall import BASH_TOOL
from minisweagent.models.utils.retry import retry
from minisweagent.utils.serialize import recursive_merge
from tenacity import stop_after_attempt

from palinreplay.core.interfaces import (
    AgentSnapshot,
    BoundaryAdapter,
    CallAdapter,
    Payload,
    ResultCapture,
)
from palinreplay.core.models import Failure
from palinreplay.core.progress import ProgressEvent
from palinreplay.diagnostics.metrics import timed
from palinreplay.diagnostics.record_timing import flow_scope, flow_timed, record_phase, record_step
from palinreplay.diagnostics.timing import span
from palinreplay.record.calls import RecordingError, deliver_batch
from palinreplay.record.recorder import Recorder
from palinreplay.storage.store import IncompleteRecordError


def public_config(value):
    """Reject secret-bearing config before it enters templates or Record."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower().endswith(
                ("api_key", "access_token", "password", "secret")
            ) or key.lower() in {
                "api_key",
                "api_base",
                "base_url",
                "authorization",
                "password",
                "secret",
                "access_token",
                "client",
                "headers",
                "extra_headers",
            }:
                raise ValueError(f"private setting {key!r} must not be in public Agent config")
            public_config(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            public_config(item)
    Payload.json(value)
    return value


@dataclass
class ModelReply:
    raw: dict
    message: dict | None = None
    format_error: FormatError | None = None


class ObservedModel(LitellmModel):
    # A storage error inside _query must escape native retry immediately.
    abort_exceptions: ClassVar = [*LitellmModel.abort_exceptions, RecordingError]

    def __init__(self, *, credentials=None, retry_attempts=10, completion=None, **config):
        public_config(config)
        if (
            not isinstance(retry_attempts, int)
            or isinstance(retry_attempts, bool)
            or retry_attempts < 1
        ):
            raise ValueError("retry_attempts must be a positive integer")
        config.setdefault("litellm_model_registry", None)
        config.setdefault("cost_tracking", "default")
        if credentials is not None:
            config["model_kwargs"] = {
                **config.get("model_kwargs", {}),
                "timeout": credentials.timeout,
                "max_retries": credentials.max_retries,
            }
        if config.get("model_kwargs", {}).get("stream"):
            raise ValueError("streaming requires a separate adapter")
        super().__init__(**config)
        self.retry_attempts = retry_attempts
        self._credentials, self._completion = credentials, completion
        self.session = None
        self.call = None
        self.raw = None
        self.cost_known = True
        self.accounting = []
        self.client_requests = 0
        self.stats = GlobalModelStats()
        # Per-run statistics, including the upstream global-limit check. Never
        # replace the module global used by unrelated mini runs.
        native_globals = {
            **LitellmModel.query.__globals__,
            "GLOBAL_MODEL_STATS": self.stats,
            "retry": self._retry,
        }
        function = FunctionType(
            LitellmModel.query.__code__,
            native_globals,
            LitellmModel.query.__name__,
            LitellmModel.query.__defaults__,
        )
        self._native_query = MethodType(function, self)

    def _retry(self, **kwargs):
        policy = retry(**kwargs).copy(stop=stop_after_attempt(self.retry_attempts))

        def before_sleep(state):
            policy.before_sleep(state)
            self.session.progress(ProgressEvent(
                "retry", step=self.session.active.step.index, attempt=state.attempt_number + 1
            ))

        def sleep(delay):
            with span(
                "model.retry_wait", step=self.session.active.step.index,
                attempt=self._request_attempt + 1,
            ):
                return policy.sleep(delay)

        return policy.copy(before_sleep=before_sleep, sleep=sleep)

    def _redact_error(self, error):
        if self._credentials is None:
            return
        private = (self._credentials.api_key.get_secret_value(), self._credentials.base_url)

        def clean(value):
            if isinstance(value, str):
                for token in private:
                    value = value.replace(token, "[private]")
            return value

        seen = set()
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            error.args = tuple(clean(value) for value in error.args)
            if isinstance(getattr(error, "message", None), str):
                error.message = clean(error.message)
            error = error.__cause__ or error.__context__

    def _query(self, messages, **kwargs):
        request = {
            "model": self.config.model_name,
            "messages": messages,
            "tools": [BASH_TOOL],
            **(self.config.model_kwargs | kwargs),
        }
        public_config(request)
        self.call.observe("request", Payload.json(request))
        self.client_requests += 1
        self._request_attempt += 1
        self.session.progress(ProgressEvent(
            "model", step=self.session.active.step.index,
            request=self.client_requests, attempt=self._request_attempt,
        ))
        try:
            with span(
                "model.request", step=self.session.active.step.index,
                request=self.client_requests, attempt=self._request_attempt,
            ):
                if self._completion is not None:
                    response = self._completion(**request)
                else:
                    private = {}
                    if self._credentials is not None:
                        private = {
                            "api_key": self._credentials.api_key.get_secret_value(),
                            "api_base": self._credentials.base_url,
                        }
                    response = super()._query(messages, **(kwargs | private))
        except BaseException as error:
            self._redact_error(error)
            # SDK errors can contain credentials/headers; internal diagnostics
            # need only the type/status, not an arbitrary client repr.
            self.call.observe(
                "error",
                Payload.json(
                    {
                        "type": type(error).__name__,
                        "status_code": getattr(error, "status_code", None),
                    }
                ),
            )
            raise
        self.call.observe("response", response, encode=self._encode_response)
        self.session.progress(ProgressEvent("model_returned", step=self.session.active.step.index))
        return response

    def _encode_response(self, response):
        self.raw = response.model_dump(mode="json")
        return Payload.json(self.raw)

    def _calculate_cost(self, response):
        result = super()._calculate_cost(response)
        known = result["cost"] > 0
        self.cost_known = self.cost_known and known
        self.accounting.append({"known": known, "cost": result["cost"] if known else None})
        return result

    def _execute_native(self, request):
        self.raw = None
        try:
            message = self._native_query(request["messages"])
        except FormatError as error:
            # The provider returned normally. Parsing is Agent control flow,
            # distinct from the model Execution's returned result.
            return ModelReply(self.raw, format_error=error)
        return ModelReply(self.raw, message=message)

    @flow_timed("2.2")
    def query(self, messages, **kwargs):
        if kwargs:
            raise ValueError("the pinned DefaultAgent query boundary has no extra kwargs")
        session = self.session
        with flow_scope("2.1", part="2.1.2"):
            session.active = session.recorder.begin_step()
        self._request_attempt = 0
        adapter = CallAdapter(
            name="mini.litellm.query",
            execute=self._execute_native,
            capture_request=Payload.json,
            capture_context=lambda request: Payload.json(request["messages"]),
            capture_result=lambda reply: ResultCapture(
                Payload.json(reply.raw),
                "litellm-sdk-response-json",
                details={
                    "parse": "format-error" if reply.format_error else "actions",
                    "accounting": self.accounting[-1] if self.accounting else None,
                },
                limitations=("SDK representation, not HTTP wire bytes or hidden HTTP retries.",),
            ),
            visibility="opaque-retries",
        )
        request = {
            "messages": deepcopy(messages),
            "config": self.config.model_dump(mode="json"),
            "retry_attempts": self.retry_attempts,
        }
        self.call = session.active.model(adapter, request)
        reply = self.call.invoke()
        session.model_reply = reply
        session.model_pending = True
        if reply.format_error:
            raise reply.format_error
        return reply.message


class BorrowedEnvironment:
    """Uses the existing Sandbox, without starting/resetting/owning a container."""

    def __init__(self, sandbox, **config):
        info = sandbox.inspect()
        allowed = {"cwd", "env", "forward_env", "timeout", "interpreter"}
        if config.keys() - allowed:
            raise ValueError("borrowed environment only accepts execution settings")
        values = {
            "image": info.actual_image_id,
            "cwd": info.workdir,
            "env": info.environment,
            "interpreter": list(info.shell),
            "timeout": 60,
        }
        values.update(config)
        values["env"] = {**info.environment, **config.get("env", {})}
        public_config(values)
        self.config = DockerEnvironmentConfig(**values)
        if tuple(self.config.interpreter) != info.shell:
            raise ValueError("mini interpreter must match the started Sandbox shell")
        self.sandbox, self.session = sandbox, None
        # Freeze the resolved forwarded variables once; no credentials are
        # discovered implicitly. Explicit env wins as in upstream.
        self.environment = {
            key: os.environ[key] for key in self.config.forward_env if key in os.environ
        }
        self.environment.update(self.config.env)
        public_config(self.environment)
        self.config.env = self.environment
        self.config.forward_env = []

    def get_template_vars(self, **kwargs):
        return recursive_merge(self.config.model_dump(), platform.uname()._asdict(), kwargs)

    def serialize(self):
        return {
            "info": {
                "config": {
                    "environment": self.config.model_dump(mode="json"),
                    "environment_type": "palinreplay.BorrowedEnvironment",
                }
            }
        }

    @staticmethod
    def output(result):
        # Match subprocess.run(text=True, encoding='utf-8', errors='replace').
        return {
            "output": result.text.replace("\r\n", "\n").replace("\r", "\n"),
            "returncode": result.exit_code,
            "exception_info": "",
        }

    def execute(self, action, cwd="", *, timeout=None):
        session = self.session
        with flow_scope(
            "2.4", step=session.active.step.index, tool=len(session.tool_calls) + 1,
            tools=len(session.model_reply.message["extra"]["actions"]),
        ):
            return self._execute_recorded(action, cwd, timeout=timeout)

    def _execute_recorded(self, action, cwd="", *, timeout=None):
        session = self.session
        request = {
            "action": deepcopy(action),
            "cwd": cwd or self.config.cwd,
            "env": self.environment,
            "interpreter": self.config.interpreter,
            "timeout_sec": timeout or self.config.timeout,
        }
        adapter = CallAdapter(
            name="mini.bash",
            capture_request=Payload.json,
            execute=lambda req: self.sandbox.exec(
                req["action"].get("command", ""),
                cwd=req["cwd"],
                env=req["env"],
                timeout_sec=req["timeout_sec"],
            ),
            capture_result=lambda result: ResultCapture(
                Payload(result.output),
                "sandbox-merged-output-bytes",
                details={"decoded": self.output(result), "duration_sec": result.duration_sec},
                outcome="error" if result.exit_code else "returned",
            ),
            visibility="observed-attempt",
        )
        call = session.active.tool(adapter, request, tool_call_id=action["tool_call_id"])
        session.tool_calls.append(call)
        counters = {
            "step": session.active.step.index,
            "tool": len(session.tool_calls),
            "tools": len(session.model_reply.message["extra"]["actions"]),
        }
        session.progress(ProgressEvent("tool", **counters))
        with span("tool.call", step=counters["step"], tool=counters["tool"]):
            output = self.output(call.invoke())
        session.progress(ProgressEvent("tool_returned", **counters))
        try:
            DockerEnvironment._check_finished(self, output)
        except Submitted:
            session.submission_call = call
            raise
        return output


def adopt_messages(messages, adoptions, deliveries, offered, receive):
    """Record native batch admission; shared by recording and local Replay."""
    start = len(messages)
    result = deliver_batch(deliveries, encode=Payload.json, receive=receive)
    offset = start
    for call, batch in deliveries:
        stop = offset + len(batch)
        event = call.accept({"start": offset, "stop": stop, "messages": messages[offset:stop]},
                            encode=Payload.json)
        adoptions.append({"execution_id": call.execution_id, "event_sequence": event.sequence,
                          "start": offset, "stop": stop})
        offset = stop
    if offset != len(messages) or offset != start + len(offered):
        raise ValueError("unexpected native message batch")
    return result


class RecordingAgent(DefaultAgent):
    """Keep DefaultAgent.run/query/execute_actions and their exception handlers."""

    def __init__(
        self, model, env, *, store, task, info, capture_state, record_metadata=None,
        progress, **config
    ):
        # Paths in AgentConfig are normalized by Pydantic; the supplied public
        # config is JSON-only except its explicitly supported output_path.
        public_config({k: v for k, v in config.items() if k != "output_path"})
        super().__init__(model, env, **config)
        model.session = env.session = self
        self.progress = progress
        self.active = None
        self.model_reply = None
        self.model_pending = False
        self.tool_calls = []
        self.submission_call = None
        self.adoptions = []
        self.capture_timings = []
        self._anchor = self._start_time
        self._boundary_pending = False
        self._step_error = None
        self._in_run = False
        self._initial_timing = None

        def capture(store, boundary, previous):
            self.progress(ProgressEvent("capturing", step=boundary.index))
            state = capture_state(store, boundary, previous)
            self.progress(ProgressEvent("committing", step=boundary.index))
            return state

        self.recorder = Recorder(
            store,
            task_id=task.task_id,
            agent_name=info.name,
            agent_version=info.version,
            config=Payload.json(
                {
                    "adapter": asdict(info),
                    "agent": self.config.model_dump(mode="json"),
                    "model": model.config.model_dump(mode="json"),
                    "environment": env.config.model_dump(mode="json"),
                    "retry_attempts": model.retry_attempts,
                    "record_metadata": public_config(record_metadata or {}),
                }
            ),
            boundary=BoundaryAdapter(lambda boundary: self.export_snapshot(), capture),
        )

    @timed("agent.export_snapshot")
    def export_snapshot(self):
        stats = self.model.stats
        return AgentSnapshot(
            context=Payload.json(
                {
                    "messages": self.messages,
                    "extra_template_vars": self.extra_template_vars,
                    "config": self.serialize()["info"]["config"],
                    "template_vars": self.get_template_vars(
                        elapsed_seconds=int(self._anchor - self._start_time)
                    ),
                    "counters": {
                        "n_calls": self.n_calls,
                        "cost": self.cost,
                        "consecutive_format_errors": self.n_consecutive_format_errors,
                    },
                    "clock": {
                        "started_at": self._start_time,
                        "boundary_at": self._anchor,
                        "elapsed_seconds": int(self._anchor - self._start_time),
                    },
                    "model_accounting": {
                        "scope": "native-returned-response-estimate",
                        "cost_known": self.model.cost_known
                        and self.model.client_requests == len(self.model.accounting),
                        "calls": self.model.accounting,
                        "client_requests": self.model.client_requests,
                        "unaccounted_client_entries": self.model.client_requests
                        - len(self.model.accounting),
                    },
                    "global_stats": {
                        "n_calls": stats.n_calls,
                        "cost": stats.cost,
                        "call_limit": stats.call_limit,
                        "cost_limit": stats.cost_limit,
                    },
                    "prior_capture_timings": self.capture_timings,
                }
            ),
            input_position=Payload.json(
                {"next_message": len(self.messages), "adoptions": self.adoptions}
            ),
        )

    def _capture(self, initial=False):
        self._anchor = time.time()
        started = time.monotonic()
        index = 0 if initial else self.active.step.index
        with record_step(index), span("node.capture_commit", step=index):
            node = self.recorder.start() if initial else self.recorder.commit_step()
        self.capture_timings.append(
            {"node_id": node.node_id, "index": node.index,
             "duration_sec": time.monotonic() - started,
             "io": self.recorder.last_boundary_metrics}
        )
        self.active = None
        self.progress(ProgressEvent("committed", step=node.index))
        if initial:
            record_phase("steps")

    def _finish_initial_timing(self, error=None):
        timing, self._initial_timing = self._initial_timing, None
        if timing is not None:
            timing.__exit__(type(error) if error else None, error, None)

    @flow_timed("2.1", part="2.1.1")
    def query(self):
        return super().query()

    @flow_timed("2.5", part="2.5.1")
    def execute_actions(self, message):
        return super().execute_actions(message)

    @timed("agent.step")
    def step(self):
        if not self._in_run:
            raise RuntimeError("drive mini through the adapter's run()")
        self._step_error = None
        self.tool_calls, self.submission_call = [], None
        try:
            if self.recorder.phase == "new":
                self._finish_initial_timing()
                self._capture(initial=True)
            with record_step(self.recorder.current_node.index + 1):
                return super().step()
        except BaseException as error:
            self._step_error = error
            raise
        finally:
            # The native run's finally/save happens AFTER exception messages
            # and format-error counters have been adopted/updated.
            self._boundary_pending = True

    @timed("agent.adopt")
    def _adopt(self, deliveries, messages):
        try:
            return adopt_messages(
                self.messages, self.adoptions, deliveries, messages,
                lambda offered: super(RecordingAgent, self).add_messages(*messages),
            )
        except ValueError as error:
            self.recorder._abort("adoption-map", error)

    def add_messages(self, *messages):
        if self.recorder.phase in {"step", "ready"}:
            index = self.active.step.index if self.active else self.recorder.current_node.index + 1
            part = None if self.model_pending else (
                "2.5.2" if self.tool_calls and self._step_error is None else "2.5.3"
            )
            with flow_scope("2.3" if self.model_pending else "2.5", step=index, part=part):
                return self._add_recorded_messages(*messages)
        return self._add_recorded_messages(*messages)

    def _add_recorded_messages(self, *messages):
        if self.recorder.phase != "step":
            return super().add_messages(*messages)
        if self.model_pending:
            result = self._adopt([(self.model.call, list(messages))], messages)
            self.model_pending = False
            actions = (self.model_reply.message or {}).get("extra", {}).get("actions", [])
            self.active.declare_tools(
                self.model.call, tuple(action["tool_call_id"] for action in actions)
            )
            return result
        if self.submission_call is not None and isinstance(self._step_error, Submitted):
            result = self._adopt([(self.submission_call, list(messages))], messages)
            self.submission_call = None
            return result
        if self.tool_calls and self._step_error is None:
            if len(messages) != len(self.tool_calls) or any(
                message.get("tool_call_id") != call.operation.tool_call_id
                for message, call in zip(messages, self.tool_calls, strict=False)
            ):
                self.recorder._abort(
                    "adoption-map", ValueError("native tool message group differs")
                )
            return self._adopt(
                [
                    (call, [message])
                    for call, message in zip(self.tool_calls, messages, strict=True)
                ],
                messages,
            )
        return super().add_messages(*messages)

    def save(self, path, *extra_dicts):
        pending, self._boundary_pending = self._boundary_pending, False
        index = self.active.step.index if self.active else (
            self.recorder.current_node.index + 1 if self.recorder.current_node else None
        )
        scope = flow_scope("2.5", step=index, part="2.5.4") if index is not None else nullcontext()
        with scope, span("agent.native_save"):
            result = super().save(path, *extra_dicts)
        if self._in_run and pending and self.recorder.phase == "step":
            if self._step_error is not None and not isinstance(
                self._step_error, (FormatError, Submitted)
            ):
                return result
            try:
                with flow_scope("2.6", step=index, part="2.6.1"):
                    self.recorder.store.accepted_step(self.active.step.step_id)
            except IncompleteRecordError:
                if isinstance(self._step_error, Submitted):
                    # Native list-comprehension execution short-circuited. No
                    # observations are invented for earlier/later tool calls.
                    return result
                raise
            self._capture()
        return result

    def _end_details(self):
        self._anchor = time.time()
        snapshot = self.export_snapshot()
        return {
            "agent_exit_status": (
                self.messages[-1].get("extra", {}).get("exit_status") if self.messages else None
            ),
            "record_complete": self.recorder.phase == "ready",
            "final_control_context": json.loads(snapshot.context.data),
            "final_input_position": json.loads(snapshot.input_position.data),
            "capture_timings": self.capture_timings,
        }

    def run(self, task="", **kwargs):
        self._in_run = True
        primary = None
        self._initial_timing = flow_scope("1.5")
        try:
            self._initial_timing.__enter__()
            result = super().run(task, **kwargs)
            self._finish_initial_timing()
            record_phase("finalization")
            with flow_scope("3.1", part="3.1.1"):
                details = self._end_details()
                if self.recorder.phase == "step":
                    self.recorder.end_incomplete(
                        "native Agent ended with an incomplete tool group", details=details
                    )
                elif result.get("exit_status") in ("LimitsExceeded", "TimeExceeded"):
                    self.recorder.pause("native Agent budget reached", details=details)
                else:
                    self.recorder.finish("native Agent ended", details=details)
            return result
        except BaseException as error:
            primary = error
            self._finish_initial_timing(error)
            record_phase("finalization")
            with flow_scope("3.1", part="3.1.2"):
                # Core recording failures may already have closed RunEnd. Persist
                # the native final control context separately without rewriting it.
                try:
                    self.recorder.store.save(
                        Failure(
                            run_id=self.recorder.run.run_id,
                            attempt_id=self.recorder.attempt.attempt_id,
                            step_id=self.active.step.step_id if self.active else None,
                            category="recording" if isinstance(error, RecordingError) else "execution",
                            details=self.recorder.store.put_json(
                                {"phase": "mini-termination", **self._end_details()}
                            ),
                        )
                    )
                except BaseException as diagnostic:
                    error.add_note(
                        f"mini final context could not be saved: {type(diagnostic).__name__}"
                    )
            raise
        finally:
            self._in_run = False
            self._finish_initial_timing(primary)
            with flow_scope("3.1", part="3.1.3"):
                self.recorder.__exit__(type(primary), primary, None)
