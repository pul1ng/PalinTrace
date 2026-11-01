"""A local historical-answer source; no model client or credentials."""

from copy import deepcopy

from palinreplay.core.interfaces import CallAdapter, Payload, ResultCapture


def supply_history(step, history, messages, adopt):
    model = history.model
    adapter = CallAdapter(
        name="replay.history",
        execute=lambda request: deepcopy(list(model.messages)),
        capture_request=Payload.json,
        capture_context=lambda request: Payload.json(request["messages"]),
        capture_result=lambda value: ResultCapture(
            Payload.json(value), "local-historical-accepted-messages",
            details={"source": model.source, "actual_model_calls": 0, "actual_model_cost": 0},
        ),
        visibility="observed-attempt",
    )
    call = step.model(adapter, {"source": model.source, "messages": deepcopy(messages)})
    reply = call.invoke()
    adopt([(call, reply)], reply)
    step.declare_tools(call, history.tool_ids)
    return call
