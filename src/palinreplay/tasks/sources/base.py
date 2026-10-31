"""Tiny source-adapter contract: a new dataset is one module plus one registry call.

An adapter only translates one raw row into a Draft. Selection, staging,
checksums, atomic publishing and package loading stay in the core pipeline, so
adding a dataset never requires touching the converter, CLI or storage format.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from palinreplay.tasks.model import DatasetSource, EnvironmentSpec, RepositorySpec

Arch = Literal["x86_64", "arm64"]
Row = Mapping[str, Any]


@dataclass(frozen=True)
class BuildOptions:
    arch: Arch = "x86_64"
    image_map: Mapping[str, str] | None = None


@dataclass(frozen=True)
class Draft:
    """Everything adapter-specific about one task; core handles the rest."""

    instruction: str
    environment: EnvironmentSpec
    repository: RepositorySpec | None = None
    evaluation_kind: str = "source-record"
    # Relative to evaluation/; values are file text. Source rows go here, never to agent input.
    evaluation_files: Mapping[str, str] = field(default_factory=lambda: {"source.json": "{}"})


def _unavailable(*args: Any, **kwargs: Any) -> Any:
    raise NotImplementedError("this source adapter does not support remote fetching")


@dataclass(frozen=True)
class SourceSpec:
    name: str
    row_id: Callable[[Row], str]
    build: Callable[[Row, BuildOptions], Draft]
    fetch: Callable[[str, str, str | None], tuple[Sequence[Row], DatasetSource]] = _unavailable
    aliases: tuple[str, ...] = ()
    extra: str | None = None
    # Canonical remote dataset for this adapter; --online uses it without extra flags.
    dataset: str | None = None


_REGISTRY: dict[str, SourceSpec] = {}


def register(spec: SourceSpec) -> SourceSpec:
    for name in (spec.name, *spec.aliases):
        if name in _REGISTRY:
            raise ValueError(f"duplicate source adapter name: {name}")
        _REGISTRY[name] = spec
    return spec


def resolve(name: str) -> SourceSpec:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown task source {name!r}; available: {', '.join(sorted(_REGISTRY))}"
        ) from None


def available() -> list[SourceSpec]:
    """Unique adapters sorted by name, independent of how many aliases each has."""
    unique: dict[int, SourceSpec] = {id(spec): spec for spec in _REGISTRY.values()}
    return sorted(unique.values(), key=lambda spec: spec.name)
