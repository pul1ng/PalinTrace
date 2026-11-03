"""Build the protected configuration for a source-reference Replay."""

from palinreplay.replay.services import PreparationError


def replay_config(materials, *, preparation=None):
    """Retain the pinned source and consumed prefix without creating a local boundary."""
    if preparation is not None and preparation["status"] != "ready":
        raise PreparationError("service preparation must be ready before Replay initialization")
    plan = materials.plan
    replay = {
        "schema_version": "palinreplay.replay.v2",
        "initial_state_mode": "source-reference-v1",
        "node_numbering": "source-index-v1",
        "execution_policy": "historical-exit-code-v1",
        "source_start_index": plan.start.index,
        "source_end_index": plan.end.index,
        "source": plan.provenance(),
        "source_start": {
            "node_id": plan.start.node_id,
            "state_id": plan.start.state_id,
            "context": materials.context,
            "input_position": materials.input_position,
        },
        "used_history": [step.document() for step in materials.steps],
        "preparation_state_captured": False,
    }
    if preparation is not None:
        replay["preparation"] = {"config": preparation["config"], "evidence": preparation}
    return {**materials.config, "replay": replay}
