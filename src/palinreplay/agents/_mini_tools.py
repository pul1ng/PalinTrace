"""Tool recording helpers shared by Record, Replay and Resume."""

from palinreplay.core.interfaces import CallAdapter, Payload, ResultCapture


def encode_tool_result(result, decoded, outcome):
    """Capture the observed bytes and caller-supplied interpretation without executing."""
    return ResultCapture(
        Payload(result.output), "sandbox-merged-output-bytes",
        details={"decoded": decoded, "duration_sec": result.duration_sec},
        outcome=outcome,
    )


def make_tool_adapter(name, execute, capture_result):
    """Wrap one caller-owned execution entry with the existing recording contract."""
    return CallAdapter(
        name=name, execute=execute, capture_request=Payload.json,
        capture_result=capture_result, visibility="observed-attempt",
    )
