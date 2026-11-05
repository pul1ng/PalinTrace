"""Service configuration at a saved Resume boundary."""

from palinreplay.replay.service_config import load_service_input


def load_resume_service_input(path):
    """Read an optional Replay-compatible service configuration."""
    return None if path is None else load_service_input(path)
