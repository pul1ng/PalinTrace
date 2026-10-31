"""Canonical JSON encoding used by stored materials."""

import json


def json_bytes(value) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
