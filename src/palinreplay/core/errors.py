"""Errors shared by recording, storage and command entry points."""

class StoreError(Exception):
    """Base error for a Record store."""


class IntegrityError(StoreError):
    """A record or its required material is missing, corrupt or inconsistent."""


class ConflictError(StoreError):
    """An immutable identity or the current execution history would be overwritten."""


class IncompleteRecordError(StoreError):
    """The declared node boundary does not have all required facts/material."""


class RecordUsageError(ValueError):
    """An actionable, credential-free input error safe to display in the CLI."""
