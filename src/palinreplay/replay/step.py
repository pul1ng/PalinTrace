"""Record one actual historical step; scheduling and policy live above this seam."""

from palinreplay.core.interfaces import CallAdapter, Payload, ResultCapture
from palinreplay.replay.history import supply_history


def execute_step(recorder, context, executor, history):
    step = recorder.begin_step()
    supply_history(step, history, context.messages, context.adopt)
    calls, results = [], []
    for tool in history.tools:
        adapter = CallAdapter(
            name="mini.bash", execute=executor.execute, capture_request=Payload.json,
            capture_result=lambda result: ResultCapture(
                Payload(result.output), "sandbox-merged-output-bytes",
                details={"decoded": context.native.BorrowedEnvironment.output(result),
                         "duration_sec": result.duration_sec},
                outcome="error" if result.exit_code else "returned",
            ), visibility="observed-attempt",
        )
        call = step.tool(adapter, tool.request, tool_call_id=tool.source["tool_call_id"])
        calls.append(call)
        result = call.invoke()
        results.append(result)
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
            return recorder.commit_step(), results
    context.accept_results(history, calls, results)
    return recorder.commit_step(), results
