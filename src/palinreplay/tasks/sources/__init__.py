"""Source adapters register themselves on import; adding one module adds a dataset."""

# Imported for its registration side effect; a new source module is one line here.
from palinreplay.tasks.sources import swebench as _swebench  # noqa: F401  (registers 3 variants)
from palinreplay.tasks.sources.base import (
    Arch,
    BuildOptions,
    Draft,
    Row,
    SourceSpec,
    available,
    register,
    resolve,
)

__all__ = [
    "Arch",
    "BuildOptions",
    "Draft",
    "Row",
    "SourceSpec",
    "available",
    "register",
    "resolve",
]
