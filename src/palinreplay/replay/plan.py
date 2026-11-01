"""Pin committed Replay boundaries in one short catalog read view."""

from dataclasses import dataclass
from hashlib import sha256
from itertools import pairwise

from palinreplay.core.errors import RecordUsageError
from palinreplay.core.models import Node, Run, State
from palinreplay.core.serialization import json_bytes
from palinreplay.record.inspect import resolve_node
from palinreplay.storage.store import RecordStore


@dataclass(frozen=True)
class ReplayPlan:
    source_store: str
    run: Run
    # Includes the restored baseline; only nodes[1:] will be executed.
    nodes: tuple[Node, ...]
    states: tuple[State, ...]

    @property
    def start(self) -> Node:
        return self.nodes[0]

    @property
    def end(self) -> Node:
        return self.nodes[-1]

    def provenance(self) -> dict:
        selection = {
            "schema_version": "palinreplay.replay-source.v1",
            "source_store": self.source_store,
            "run": self.run.model_dump(mode="json"),
            "nodes": [node.model_dump(mode="json") for node in self.nodes],
            "states": [state.model_dump(mode="json") for state in self.states],
        }
        return {
            **selection,
            "digest_scheme": "sha256-canonical-selection-json-v1",
            "selection_sha256": sha256(json_bytes(selection)).hexdigest(),
        }


def pin_replay(
    store: RecordStore, start: str, end: str | None = None, *, run_id: str | None = None
) -> ReplayPlan:
    """Freeze baseline through inclusive end; ignore uncommitted tails and later appends."""
    with store.snapshot():
        first = resolve_node(store, start, run_id)
        run = store.get(Run, first.run_id)
        committed = sorted(store.list(Node, run_id=run.run_id), key=lambda n: n.index)
        last = resolve_node(store, end, run.run_id) if end is not None else committed[-1]
        if last.index < first.index:
            raise RecordUsageError("Replay end precedes its starting node")
        nodes = tuple(n for n in committed if first.index <= n.index <= last.index)
        if not nodes or nodes[0] != first or nodes[-1] != last:
            raise RecordUsageError("Replay boundaries do not belong to the selected committed range")
        if [n.index for n in nodes] != list(range(first.index, last.index + 1)) or any(
            right.parent_node_id != left.node_id for left, right in pairwise(nodes)
        ):
            raise RecordUsageError("Replay range is not a contiguous committed node chain")
        states = tuple(store.get(State, n.state_id) for n in nodes)
        return ReplayPlan(str(store.root.resolve()), run, nodes, states)
