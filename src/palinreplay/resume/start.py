"""Select a committed Resume source without restoring its materials."""

from dataclasses import dataclass
from pathlib import Path

from palinreplay.cli.record import store_path
from palinreplay.core.errors import RecordUsageError
from palinreplay.core.models import Node, Run, State
from palinreplay.record.inspect import resolve_node
from palinreplay.storage.store import RecordStore


@dataclass(frozen=True)
class _ResumeStart:
    source_store: Path
    node: Node
    model: str
    max_steps: int

    def __post_init__(self):
        if type(self.max_steps) is not int or self.max_steps <= 0:
            raise RecordUsageError("max_steps must be a positive integer")


def _select_source(
    store: str | Path, selector: str, *, run_id: str | None = None
) -> tuple[Path, Node]:
    with RecordStore.open(store_path(Path(store)), read_only=True) as source:
        with source.snapshot():
            node = resolve_node(source, selector, run_id)
            source.get(Run, node.run_id)
            source.get(State, node.state_id)
            return source.root, node
