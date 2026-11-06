"""Record one actual historical step; scheduling and policy live above this seam."""

from contextlib import nullcontext

from palinreplay.agents._mini_tools import encode_tool_result, make_tool_adapter
from palinreplay.replay.history import supply_history


def execute_step(recorder, context, executor, history, *, on_result=None, stats=None):
    step = recorder.begin_step()
    if stats:
        stats.data["historical_steps"] += 1
    context.historical_answer(history)
    supply_history(step, history, context.messages, context.adopt)
    if stats:
        stats.data["historical_answer_supplies"] += 1

    def commit():
        with stats.timed("snapshot_commit") if stats else nullcontext():
            return recorder.commit_step()

    calls, results = [], []
    for index, tool in enumerate(history.tools):
        def execute(request):
            if stats:
                stats.data["actual_tool_executions"] += 1
            with stats.timed("execution") if stats else nullcontext():
                return executor.execute(request)

        adapter = make_tool_adapter(
            name="mini.bash", execute=execute,
            capture_result=lambda result: encode_tool_result(
                result, context.native.BorrowedEnvironment.output(result),
                "unknown" if type(result.exit_code) is not int else
                "error" if result.exit_code else "returned",
            ),
        )
        call = step.tool(adapter, tool.request, tool_call_id=tool.source["tool_call_id"])
        calls.append(call)
        result = call.invoke()
        results.append(result)
        if on_result is not None:
            on_result(index, tool, call, result, index == len(history.tools) - 1)
        # Preserve mini's native singleton submission behavior. A submission in
        # the middle of a group cannot publish a complete step.
        try:
            context.native.DockerEnvironment._check_finished(
                executor, context.native.BorrowedEnvironment.output(result)
            )
        except context.native.Submitted as submitted:
            if len(history.tools) != 1:
                raise ValueError("submission short-circuited an incomplete tool group") from submitted
            context.adopt([(call, submitted.messages)], submitted.messages)
            return commit(), results
    context.accept_results(history, calls, results)
    return commit(), results
