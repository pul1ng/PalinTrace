"""Versioned preprocessing manifest and Replay-facing selection."""

import json
from itertools import pairwise
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from palinreplay.core.errors import RecordUsageError
from palinreplay.core.schema import Spec
from palinreplay.preprocess.lifecycle import ServiceLifecycle
from palinreplay.preprocess.recipes import IntervalRecipe
from palinreplay.preprocess.timeline import Timeline

SCHEMA_VERSION = "palinreplay.preprocess-services.v1"


class ManifestSource(Spec):
    run_id: StrictStr
    start_index: StrictInt = Field(ge=0)
    end_index: StrictInt = Field(ge=0)
    digest_scheme: StrictStr
    selection_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def ordered(self):
        if self.start_index > self.end_index:
            raise ValueError("manifest source start must not follow its end")
        return self


class EvidenceRef(Spec):
    index: StrictInt = Field(ge=0)
    execution_id: StrictStr

    @field_validator("execution_id")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("evidence execution_id must be nonempty")
        return value


class ManifestEvidence(Spec):
    start: EvidenceRef
    probe: EvidenceRef
    stop: EvidenceRef | None = None


class ManifestLaunch(Spec):
    start_command: StrictStr
    readiness_command: StrictStr
    cwd: StrictStr
    env: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    timeout_sec: float = Field(gt=0, allow_inf_nan=False)

    @field_validator("start_command", "readiness_command")
    @classmethod
    def commands(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("manifest commands must be nonempty without NUL")
        return value

    @field_validator("cwd")
    @classmethod
    def absolute(cls, value):
        if not value.startswith("/") or "\x00" in value:
            raise ValueError("manifest cwd must be an absolute container path")
        return value

    @field_validator("env")
    @classmethod
    def environment(cls, value):
        if any(not key or "=" in key or "\x00" in key + content
               for key, content in value.items()):
            raise ValueError("manifest env must contain valid keys and values")
        return value


class ManifestService(Spec):
    name: StrictStr
    start_inclusive: StrictInt = Field(ge=0)
    end_exclusive: StrictInt = Field(ge=0)
    launch: ManifestLaunch | None
    evidence: ManifestEvidence

    @field_validator("name")
    @classmethod
    def name_nonempty(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("service name must be nonempty without NUL")
        return value

    @model_validator(mode="after")
    def interval(self):
        if self.start_inclusive >= self.end_exclusive:
            raise ValueError("service interval must be nonempty")
        return self


class ManifestUnknown(Spec):
    name: StrictStr
    start_inclusive: StrictInt = Field(ge=0)
    end_exclusive: StrictInt = Field(ge=0)
    kind: Literal["state", "launch"]
    reason: StrictStr
    evidence: EvidenceRef | None = None

    @field_validator("name", "reason")
    @classmethod
    def nonempty(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("unknown name and reason must be nonempty without NUL")
        return value

    @model_validator(mode="after")
    def interval(self):
        if self.start_inclusive >= self.end_exclusive:
            raise ValueError("unknown interval must be nonempty")
        return self


class ServiceManifest(Spec):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    source: ManifestSource
    services: tuple[ManifestService, ...] = ()
    unknowns: tuple[ManifestUnknown, ...] = ()

    @model_validator(mode="after")
    def ranges(self):
        for service in self.services:
            if not (
                self.source.start_index <= service.start_inclusive
                and service.end_exclusive <= self.source.end_index + 1
            ):
                raise ValueError(f"service {service.name!r} lies outside the analyzed source range")
        for unknown in self.unknowns:
            if not (
                self.source.start_index <= unknown.start_inclusive
                and unknown.end_exclusive <= self.source.end_index + 1
            ):
                raise ValueError(f"unknown range for {unknown.name!r} lies outside the source range")
        by_name: dict[str, list[ManifestService]] = {}
        for service in self.services:
            by_name.setdefault(service.name, []).append(service)
        for name, items in by_name.items():
            ordered = sorted(items, key=lambda item: item.start_inclusive)
            for left, right in pairwise(ordered):
                if right.start_inclusive < left.end_exclusive:
                    raise ValueError(f"overlapping lifecycles for service {name!r}")
        return self


def _ref(event) -> EvidenceRef | None:
    if event is None:
        return None
    return EvidenceRef(index=event.node_index, execution_id=event.execution_id)


def _launch(recipe: IntervalRecipe) -> ManifestLaunch | None:
    if recipe.launch is None:
        return None
    return ManifestLaunch(**recipe.launch.model_dump(mode="json"))


def _recipe_map(lifecycles, recipes) -> dict[tuple[str, int, int], IntervalRecipe]:
    expected = [
        (lifecycle.service.name, interval.start_inclusive, interval.end_exclusive)
        for lifecycle in lifecycles
        for interval in lifecycle.intervals
    ]
    actual = [(recipe.service, recipe.interval.start_inclusive, recipe.interval.end_exclusive)
              for recipe in recipes]
    if expected != actual:
        raise ValueError("lifecycle and recipe ordering differ")
    return dict(zip(actual, recipes, strict=True))


def build_manifest(
    source: dict,
    lifecycles: tuple[ServiceLifecycle, ...],
    recipes: tuple[IntervalRecipe, ...],
    timeline: Timeline,
) -> ServiceManifest:
    """Assemble only confirmed intervals; every missing command remains unknown."""
    source_model = ManifestSource.model_validate(source)
    recipe_by_key = _recipe_map(lifecycles, recipes)
    by_execution = {
        (call.node_index, call.execution_id) for call in timeline.calls
    }
    services, unknowns = [], []
    for lifecycle in lifecycles:
        service = lifecycle.service
        for interval in lifecycle.intervals:
            recipe = recipe_by_key[
                (service.name, interval.start_inclusive, interval.end_exclusive)
            ]
            launch = None if service.identity_unknown else _launch(recipe)
            start_event = interval.start_event
            stop_event = interval.stop_event
            probe_event = recipe.probe_event or interval.probe_event
            evidence = ManifestEvidence(
                start=_ref(start_event),
                probe=_ref(probe_event),
                stop=_ref(stop_event),
            )
            for ref in (evidence.start, evidence.probe, evidence.stop):
                if ref is not None and (ref.index, ref.execution_id) not in by_execution:
                    raise ValueError(
                        f"manifest evidence {ref.execution_id!r} is not a source tool call"
                    )
            services.append(
                ManifestService(
                    name=service.name,
                    start_inclusive=interval.start_inclusive,
                    end_exclusive=interval.end_exclusive,
                    launch=launch,
                    evidence=evidence,
                )
            )
            reasons = []
            if service.identity_unknown:
                reasons.append("service identity is unresolved; the launch cannot be selected safely")
            for unknown in recipe.unknowns:
                reasons.append(unknown.reason)
            if launch is None and not reasons:
                reasons.append("no verified launch recipe was available")
            for reason in dict.fromkeys(reasons):
                unknowns.append(
                    ManifestUnknown(
                        name=service.name,
                        start_inclusive=interval.start_inclusive,
                        end_exclusive=interval.end_exclusive,
                        kind="launch",
                        reason=reason,
                        evidence=_ref(start_event),
                    )
                )
        for unknown in lifecycle.unknowns:
            unknowns.append(
                ManifestUnknown(
                    name=unknown.name,
                    start_inclusive=unknown.start_inclusive,
                    end_exclusive=unknown.end_exclusive,
                    kind="state",
                    reason=unknown.reason,
                    evidence=None,
                )
            )
    unknowns.sort(
        key=lambda item: (
            item.name, item.start_inclusive, item.end_exclusive, item.kind, item.reason
        )
    )
    return ServiceManifest(
        source=source_model,
        services=tuple(services),
        unknowns=tuple(unknowns),
    )


def load_manifest(path: str | Path) -> ServiceManifest:
    return ServiceManifest.model_validate_json(Path(path).read_bytes())


def manifest_document(manifest: ServiceManifest) -> dict:
    return manifest.model_dump(mode="json")


def write_manifest(path: str | Path, manifest: ServiceManifest) -> None:
    from palinreplay.storage.io import write_json

    write_json(Path(path), manifest_document(manifest))


def select_services(manifest: ServiceManifest, node_index: int) -> tuple[ManifestService, ...]:
    """Select services needed after Cn; reject unknowns that affect this decision."""
    if not (manifest.source.start_index <= node_index <= manifest.source.end_index):
        raise RecordUsageError(
            f"C{node_index} lies outside the manifest analysis range "
            f"C{manifest.source.start_index}-C{manifest.source.end_index}"
        )
    blockers = [
        unknown
        for unknown in manifest.unknowns
        if unknown.start_inclusive <= node_index < unknown.end_exclusive
    ]
    if blockers:
        details = "; ".join(
            f"{unknown.name} [{unknown.start_inclusive},{unknown.end_exclusive}) "
            f"{unknown.kind}: {unknown.reason}"
            for unknown in blockers
        )
        raise RecordUsageError(f"service state is unknown at C{node_index}: {details}")
    selected = tuple(
        service
        for service in manifest.services
        if service.start_inclusive <= node_index < service.end_exclusive
    )
    missing = [service.name for service in selected if service.launch is None]
    if missing:
        raise RecordUsageError(
            f"no verified launch recipe at C{node_index} for: {', '.join(missing)}"
        )
    names = [service.name for service in selected]
    if len(set(names)) != len(names):
        raise RecordUsageError(f"multiple selected lifecycles share a service name at C{node_index}")
    return selected


def manifest_json(manifest: ServiceManifest) -> str:
    return json.dumps(
        manifest_document(manifest), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
