"""Select a committed Resume source without restoring its materials."""

from pathlib import Path

from palinreplay.cli.record import store_path
from palinreplay.core.models import Node
from palinreplay.record.inspect import resolve_node
from palinreplay.storage.store import RecordStore


def _select_source(
    store: str | Path, selector: str, *, run_id: str | None = None
) -> tuple[Path, Node]:
    with RecordStore.open(store_path(Path(store)), read_only=True) as source:
        with source.snapshot():
            node = resolve_node(source, selector, run_id)
            return source.root, node
