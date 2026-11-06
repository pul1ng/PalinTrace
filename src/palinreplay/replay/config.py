"""Build the protected configuration for a source-reference Replay."""

import base64

from palinreplay.core.errors import IntegrityError
from palinreplay.restoration.services import PreparationError


def replay_config(materials, *, r1=None, preparation=None):
    """Retain the pinned selection; actual history usage is established by call events."""
    if r1 is not None and r1["status"] != "passed":
        raise IntegrityError("R1 validation must pass before Replay initialization")
    if preparation is not None and preparation["status"] != "ready":
        raise PreparationError("service preparation must be ready before Replay initialization")
    plan = materials.plan
    replay = {
        "schema_version": "palinreplay.replay.v2",
        "initial_state_mode": "source-reference-v1",
        "node_numbering": "source-index-v1",
        "execution_policy": "historical-exit-code-v1",
        "finalization_protocol": "store-receipt-v1",
        "source_start_index": plan.start.index,
        "source_end_index": plan.end.index,
        "source": plan.provenance(),
        "source_start": {
            "node_id": plan.start.node_id,
            "state_id": plan.start.state_id,
            "context": materials.context,
            "input_position": materials.input_position,
            "raw_materials": {
                "context": base64.b64encode(materials.context_bytes).decode("ascii"),
                "input_position": base64.b64encode(materials.input_position_bytes).decode("ascii"),
            },
        },
        "selected_history": [step.document() for step in materials.steps],
        "preparation_state_captured": False,
    }
    if r1 is not None:
        replay["r1"] = r1
    if preparation is not None:
        replay["preparation"] = {"config": preparation["config"], "evidence": preparation}
    return {**materials.config, "replay": replay}
