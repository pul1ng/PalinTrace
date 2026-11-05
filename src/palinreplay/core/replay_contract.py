"""The source-reference permission shared by recording and checked readers."""

import base64
import json
from dataclasses import dataclass
from hashlib import sha256
from itertools import pairwise

from palinreplay.core.errors import IntegrityError
from palinreplay.core.models import Node, Run, State
from palinreplay.core.serialization import json_bytes
from palinreplay.storage.codec import JSON_TYPES, decode


@dataclass(frozen=True)
class SourceReference:
    source_store: str
    run: Run
    nodes: tuple[Node, ...]
    states: tuple[State, ...]
    finalization_protocol: str | None = None
    mode: str = "replay"

    @property
    def start(self):
        return self.nodes[0]

    @property
    def end(self):
        return self.nodes[-1]


def source_reference(config) -> SourceReference | None:
    """Validate a declared v2 contract; unrelated Record configurations remain unrestricted."""
    replay = config.get("replay") if isinstance(config, dict) else None
    if not isinstance(replay, dict) or (
        replay.get("schema_version") != "palinreplay.replay.v2"
        and replay.get("initial_state_mode") != "source-reference-v1"
    ):
        return None
    try:
        if any(replay.get(key) != value for key, value in {
            "schema_version": "palinreplay.replay.v2",
            "initial_state_mode": "source-reference-v1",
            "node_numbering": "source-index-v1",
            "execution_policy": "historical-exit-code-v1",
        }.items()):
            raise ValueError("unsupported source-reference declaration")
        source = replay["source"]
        selection = {key: source[key] for key in ("schema_version", "source_store", "run", "nodes", "states")}
        if (source["schema_version"] != "palinreplay.replay-source.v1"
                or source["digest_scheme"] != "sha256-canonical-selection-json-v1"
                or sha256(json_bytes(selection)).hexdigest() != source["selection_sha256"]
                or not isinstance(source["source_store"], str) or not source["source_store"]):
            raise ValueError("invalid pinned selection or digest")
        run = Run.model_validate(source["run"])
        nodes = tuple(Node.model_validate(value) for value in source["nodes"])
        states = tuple(State.model_validate(value) for value in source["states"])
        if (not nodes or len(nodes) != len(states)
                or any(n.run_id != run.run_id or n.state_id != s.state_id
                       for n, s in zip(nodes, states, strict=True))
                or any(b.index != a.index + 1 or b.parent_node_id != a.node_id
                       for a, b in pairwise(nodes))):
            raise ValueError("invalid pinned node/state chain")
        for key, expected in (("source_start_index", nodes[0].index), ("source_end_index", nodes[-1].index)):
            if type(replay[key]) is not int or replay[key] != expected:
                raise ValueError("source range differs from pinned selection")
        start = replay["source_start"]
        if start["node_id"] != nodes[0].node_id or start["state_id"] != states[0].state_id:
            raise ValueError("source start differs from pinned selection")
        for key, ref in (("context", nodes[0].context), ("input_position", nodes[0].input_position)):
            raw = start.get("raw_materials")
            # Earlier v2 records copied canonical JSON only. Keep those readable;
            # new records retain the exact source encoding, including compression.
            data = json_bytes(start[key]) if raw is None else base64.b64decode(raw[key], validate=True)
            if sha256(data).hexdigest() != ref.sha256 or len(data) != ref.size:
                raise ValueError("source prefix differs from its protected material reference")
            if ref.media_type not in JSON_TYPES:
                raise ValueError("source prefix requires a supported JSON media type")
            if json_bytes(json.loads(decode(data, ref.media_type))) != json_bytes(start[key]):
                raise ValueError("source prefix differs from its decoded material")
        protocol = replay.get("finalization_protocol")
        if protocol not in (None, "store-receipt-v1"):
            raise ValueError("unsupported Replay finalization protocol")
        return SourceReference(source["source_store"], run, nodes, states, protocol)
    except (KeyError, TypeError, ValueError) as error:
        raise IntegrityError(f"invalid Replay source reference: {error}") from error
