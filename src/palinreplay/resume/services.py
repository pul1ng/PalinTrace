"""Service configuration at a saved Resume boundary."""

from palinreplay.replay.plan import pin_replay
from palinreplay.replay.service_config import load_service_input, resolve_services
from palinreplay.storage.store import RecordStore


def load_resume_service_input(path):
    """Read an optional Replay-compatible service configuration."""
    return None if path is None else load_service_input(path)


def resolve_resume_services(start, service_input):
    """Validate the complete manifest source and select only the saved start."""
    if service_input is None:
        return None
    with RecordStore.open(start.source_store, read_only=True) as store, store.snapshot():
        boundary = pin_replay(store, start.node.node_id, start.node.node_id,
                              run_id=start.node.run_id)
        return resolve_services(service_input, store, boundary)
