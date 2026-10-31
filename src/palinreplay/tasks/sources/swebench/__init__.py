"""Importing the variants registers them; shared logic lives in common.py."""

from palinreplay.tasks.sources.swebench import full, lite, verified

__all__ = ["common", "full", "lite", "verified"]
