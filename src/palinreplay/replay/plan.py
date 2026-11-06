"""Replay names for shared committed-source selection."""

from palinreplay.record.selection import SourceSelection as ReplayPlan
from palinreplay.record.selection import pin_source_selection as pin_replay

__all__ = ["ReplayPlan", "pin_replay"]
