"""A Resume branch pins one external boundary without a historical end range."""

import base64
import json
from hashlib import sha256

from palinreplay.core.errors import IntegrityError
from palinreplay.core.models import Node, Run, State
from palinreplay.core.replay_contract import SourceReference
from palinreplay.core.serialization import json_bytes
from palinreplay.storage.codec import JSON_TYPES, decode


def resume_source_reference(config) -> SourceReference | None:
    resume = config.get("resume") if isinstance(config, dict) else None
    if not isinstance(resume, dict) or (
        resume.get("schema_version") != "palinreplay.resume.v1"
        and resume.get("initial_state_mode") != "source-reference-v1"
    ):
        return None
    try:
        if (resume["schema_version"] != "palinreplay.resume.v1"
                or resume["initial_state_mode"] != "source-reference-v1"
                or resume["node_numbering"] != "source-index-v1" or "replay" in config):
            raise ValueError("unsupported or conflicting Resume source declaration")
        source = resume["source"]
        if (not isinstance(source["store"], str) or not source["store"]
                or sha256(json_bytes(source)).hexdigest() != resume["source_sha256"]):
            raise ValueError("invalid pinned source or digest")
        run = Run.model_validate(source["run"])
        node = Node.model_validate(source["node"])
        state = State.model_validate(source["state"])
        if node.run_id != run.run_id or node.state_id != state.state_id:
            raise ValueError("source Run/Node/State identities differ")
        for role, ref in (("config", run.config), ("context", node.context),
                          ("input_position", node.input_position)):
            data = base64.b64decode(resume["raw_materials"][role], validate=True)
            if (sha256(data).hexdigest() != ref.sha256 or len(data) != ref.size
                    or ref.media_type not in JSON_TYPES
                    or not isinstance(json.loads(decode(data, ref.media_type)), dict)):
                raise ValueError(f"source {role} differs from its protected material reference")
        return SourceReference(source["store"], run, (node,), (state,))
    except (KeyError, TypeError, ValueError) as error:
        raise IntegrityError(f"invalid Resume source reference: {error}") from error
