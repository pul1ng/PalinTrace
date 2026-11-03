"""Describe and check an external Replay source without making it a local parent."""

from palinreplay.core.errors import IntegrityError
from palinreplay.core.models import Run, State
from palinreplay.storage.store import RecordStore


def source_dependency(store, run_id, *, check=False):
    source = store.replay_source(run_id)
    if source is None:
        return None
    document = {
        "source_store": source.source_store, "run_id": source.run.run_id,
        "start": {"index": source.start.index, "node_id": source.start.node_id,
                  "state_id": source.start.state_id, "reference": True},
        "end": {"index": source.end.index, "node_id": source.end.node_id},
        "required": True, "checked": False,
        "materials": "source Record artifacts and retained filesystem snapshots",
    }
    if check:
        try:
            with RecordStore.open(source.source_store) as origin, origin.snapshot():
                if origin.get(Run, source.run.run_id) != source.run:
                    raise IntegrityError("source Run differs from the pinned reference")
                origin.verify_artifact(source.run.config)
                for node, state in zip(source.nodes, source.states, strict=True):
                    if origin.get_node(node.node_id) != node or origin.get(State, state.state_id) != state:
                        raise IntegrityError("source boundary differs from the pinned reference")
        except (OSError, ValueError, IntegrityError) as error:
            raise IntegrityError(f"required Replay source unavailable: {source.source_store}: {error}") from error
        document["checked"] = True
    return document
