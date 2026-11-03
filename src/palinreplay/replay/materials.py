"""Read and preflight the small historical materials actually consumed by Replay."""

import base64
import math
from dataclasses import dataclass

from palinreplay.agents.mini import MINI_COMMIT, MINI_VERSION
from palinreplay.core.errors import IntegrityError
from palinreplay.core.models import Execution, Node, Step, ToolGroup
from palinreplay.record.calls import read_call
from palinreplay.replay.plan import ReplayPlan


@dataclass(frozen=True)
class HistoricalCall:
    source: dict
    request: dict
    raw: bytes
    details: dict
    messages: tuple[dict, ...]
    delivered: tuple[dict, ...]
    outcome: str

    def document(self):
        return {
            "source": self.source,
            "request": self.request,
            "raw_base64": base64.b64encode(self.raw).decode("ascii"),
            "details": self.details,
            "messages": self.messages,
            "delivered": self.delivered,
            "outcome": self.outcome,
        }


@dataclass(frozen=True)
class HistoricalStep:
    node: Node
    model: HistoricalCall
    tools: tuple[HistoricalCall, ...]
    tool_ids: tuple[str, ...]

    def document(self):
        return {
            "node_id": self.node.node_id,
            "index": self.node.index,
            "model": self.model.document(),
            "tools": [tool.document() for tool in self.tools],
            "tool_ids": self.tool_ids,
        }


@dataclass(frozen=True)
class ReplayMaterials:
    plan: ReplayPlan
    config: dict
    context: dict
    input_position: dict
    steps: tuple[HistoricalStep, ...]
    context_bytes: bytes
    input_position_bytes: bytes


def _call(store, node, execution_id, kind):
    trace = read_call(store, execution_id)
    operation, execution, events = trace.operation, trace.execution, trace.events
    if (
        operation.run_id != node.run_id or operation.step_id != node.step_id
        or operation.kind != kind or trace.phase != "accepted" or len(events) != 4
        or [e.kind for e in events] != ["dispatched", "result", "delivered", "accepted"]
        or events[-1].sequence > node.accepted_event_sequence
    ):
        raise IntegrityError("historical call does not match its committed boundary")
    attempts = [e for e in store.list(Execution, run_id=node.run_id)
                if e.operation_id == operation.operation_id]
    if len(attempts) != 1 or execution.index != 1:
        raise IntegrityError("Replay does not support multiple recorded execution attempts")
    accepted = store.read_json(events[-1].payload)
    offered = store.read_json(events[-2].payload)
    if (
        not isinstance(accepted, dict) or set(accepted) != {"start", "stop", "messages"}
        or not isinstance(accepted["messages"], list)
        or not all(isinstance(m, dict) for m in accepted["messages"])
        or not isinstance(accepted["start"], int)
        or accepted["stop"] != accepted["start"] + len(accepted["messages"])
        or accepted["messages"] != offered
    ):
        raise IntegrityError("unsupported mini message adoption envelope")
    details = store.read_json(events[1].details)
    if events[1].outcome == "unknown" or details.get("exception") is not None:
        raise IntegrityError("historical execution has no definitive result")
    # Model request messages contain historical future context; they are not used
    # as Replay input. read_call checks their integrity without copying them.
    request = store.read_json(execution.request) if kind == "tool" else {}
    source = {
        "operation_id": operation.operation_id, "execution_id": execution_id,
        "name": operation.name, "tool_call_id": operation.tool_call_id,
        "raw_ref": events[1].payload.model_dump(mode="json"),
        "accepted_ref": events[-1].payload.model_dump(mode="json"),
        "accepted_sequence": events[-1].sequence,
        "start": accepted["start"], "stop": accepted["stop"],
    }
    return HistoricalCall(source, request, store.read_bytes(events[1].payload), details,
                          tuple(accepted["messages"]), tuple(offered), events[1].outcome)


def _tool_request(call, action):
    req = call.request
    if set(req) != {"action", "cwd", "env", "interpreter", "timeout_sec"}:
        raise IntegrityError("unsupported historical tool execution request")
    if (
        call.source["name"] != "mini.bash" or req["action"] != action
        or not isinstance(action.get("command"), str) or "\0" in action["command"]
        or not isinstance(req["cwd"], str) or not req["cwd"].startswith("/")
        or not isinstance(req["env"], dict)
        or not all(isinstance(k, str) and k and "=" not in k and "\0" not in k
                   and isinstance(v, str) and "\0" not in v for k, v in req["env"].items())
        or not isinstance(req["interpreter"], list) or not req["interpreter"]
        or not all(isinstance(v, str) and v and "\0" not in v for v in req["interpreter"])
        or type(req["timeout_sec"]) not in (int, float)
        or not math.isfinite(req["timeout_sec"]) or req["timeout_sec"] <= 0
    ):
        raise IntegrityError("unsupported historical command, environment, shell or timeout")
    decoded = call.details.get("backend", {}).get("decoded", {})
    code = decoded.get("returncode")
    if (type(code) is not int or call.outcome != ("error" if code else "returned")
            or decoded.get("output") != call.raw.decode("utf-8", errors="replace")
            .replace("\r\n", "\n").replace("\r", "\n")):
        raise IntegrityError("historical tool result has no consistent exit code and raw output")


def load_replay_materials(store, plan: ReplayPlan) -> ReplayMaterials:
    """Preflight the entire pinned range before any historical tool may execute."""
    with store.snapshot():
        if (plan.run.agent_name != "mini-swe-agent"
                or plan.run.agent_version != f"{MINI_VERSION}+{MINI_COMMIT}"):
            raise IntegrityError("Replay requires the pinned mini-swe-agent version")
        config = store.read_json(plan.run.config)
        context = store.read_json(plan.start.context)
        position = store.read_json(plan.start.input_position)
        if (not isinstance(context.get("messages"), list)
                or position.get("next_message") != len(context["messages"])
                or not isinstance(position.get("adoptions"), list)):
            raise IntegrityError("unsupported starting mini context or input position")
        if not all(key in config for key in ("agent", "model", "environment")):
            raise IntegrityError("missing saved mini execution configuration")
        next_message = len(context["messages"])
        steps = []
        for node in plan.nodes[1:]:
            # Validate the historical cursor, not the run's current cursor.
            store._step_records(store.get(Step, node.step_id), node)
            model = _call(store, node, node.model_execution_id, "model")
            groups = [g for g in store.list(ToolGroup, run_id=node.run_id)
                      if g.step_id == node.step_id]
            if len(groups) != 1 or groups[0].model_execution_id != node.model_execution_id:
                raise IntegrityError("missing or inconsistent historical tool group")
            ids = groups[0].tool_call_ids
            actions = [a for m in model.messages for a in m.get("extra", {}).get("actions", [])]
            if tuple(a.get("tool_call_id") for a in actions) != ids:
                raise IntegrityError("accepted model actions differ from the historical tool group")
            if model.source["name"] != "mini.litellm.query":
                raise IntegrityError("unsupported historical model adapter")
            tools = tuple(_call(store, node, eid, "tool") for eid in node.tool_execution_ids)
            if len(tools) != len(ids):
                raise IntegrityError("historical tool count differs from declared group")
            for tool, action, tool_id in zip(tools, actions, ids, strict=True):
                if tool.source["tool_call_id"] != tool_id:
                    raise IntegrityError("historical tool order differs from accepted actions")
                _tool_request(tool, action)
            for call in (model, *tools):
                if call.source["start"] != next_message:
                    raise IntegrityError("historical message adoption is not a contiguous prefix")
                next_message = call.source["stop"]
            steps.append(HistoricalStep(node, model, tools, ids))
        return ReplayMaterials(plan, config, context, position, tuple(steps),
                               store.read_bytes(plan.start.context),
                               store.read_bytes(plan.start.input_position))
