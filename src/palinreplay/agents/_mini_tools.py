"""Tool recording helpers shared by Record, Replay and Resume."""

from palinreplay.core.interfaces import Payload, ResultCapture


def encode_tool_result(result, decoded, outcome):
    """Capture the observed bytes and caller-supplied interpretation without executing."""
    return ResultCapture(
        Payload(result.output), "sandbox-merged-output-bytes",
        details={"decoded": decoded, "duration_sec": result.duration_sec},
        outcome=outcome,
    )
