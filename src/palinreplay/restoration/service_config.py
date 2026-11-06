"""Explicit service definitions; never infer services from recorded history."""

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, StrictStr, field_validator

from palinreplay.core.errors import RecordUsageError
from palinreplay.core.models import Node
from palinreplay.core.schema import Spec
from palinreplay.preprocess.manifest import (
    SUPPORTED_SCHEMA_VERSIONS,
    ServiceManifest,
    select_services,
)
from palinreplay.record.selection import pin_source_selection
from palinreplay.storage.store import RecordStore


class ServiceSpec(Spec):
    name: StrictStr
    start_command: StrictStr
    readiness_command: StrictStr | None = None
    timeout_sec: float = Field(strict=True, gt=0, allow_inf_nan=False)
    cwd: StrictStr | None = None
    env: dict[StrictStr, StrictStr] = Field(default_factory=dict)

    @field_validator("name", "start_command", "readiness_command")
    @classmethod
    def nonempty(cls, value):
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("service names and commands must be nonempty and contain no NUL")
        return value

    @field_validator("cwd")
    @classmethod
    def workdir(cls, value):
        if value is not None and (not value.startswith("/") or "\x00" in value):
            raise ValueError("cwd must be an absolute container path without NUL")
        return value

    @field_validator("env")
    @classmethod
    def environment(cls, value):
        if any(not key or "=" in key or "\x00" in key + content
               for key, content in value.items()):
            raise ValueError("env must contain valid environment keys and values")
        return value


class ServicesConfig(Spec):
    services: tuple[ServiceSpec, ...]

    @field_validator("services")
    @classmethod
    def unique_names(cls, value):
        if len({service.name for service in value}) != len(value):
            raise ValueError("service names must be unique")
        return value


def load_services(path: str | Path) -> ServicesConfig:
    """Validate a legacy flat file without opening a recording or creating resources."""
    return ServicesConfig.model_validate_json(Path(path).read_bytes())


@dataclass(frozen=True)
class ServiceInput:
    """Exactly one of a legacy static list or a versioned preprocess manifest."""

    static: ServicesConfig | None
    manifest: ServiceManifest | None
    path: Path


def load_service_input(path: str | Path) -> ServiceInput:
    """Parse either format; a versioned file never falls back to static parsing."""
    path = Path(path)
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise RecordUsageError(f"invalid service configuration JSON: {error}") from error
    if not isinstance(document, dict):
        raise RecordUsageError("service configuration must be a JSON object")
    if "schema_version" in document:
        version = document["schema_version"]
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise RecordUsageError(
                f"unsupported service manifest schema_version {version!r}; "
                f"expected one of {SUPPORTED_SCHEMA_VERSIONS!r}"
            )
        try:
            manifest = ServiceManifest.model_validate(document)
        except ValueError as error:
            raise RecordUsageError(f"invalid service manifest: {error}") from error
        return ServiceInput(static=None, manifest=manifest, path=path)
    try:
        static = ServicesConfig.model_validate(document)
    except ValueError as error:
        raise RecordUsageError(f"invalid static service configuration: {error}") from error
    return ServiceInput(static=static, manifest=None, path=path)


def _validate_manifest_source(
    store: RecordStore, start: Node, end_index: int, manifest: ServiceManifest
) -> None:
    source = manifest.source
    if source.run_id != start.run_id:
        raise RecordUsageError(
            f"service manifest belongs to Run {source.run_id!r}, not {start.run_id!r}"
        )
    if source.start_index > start.index or end_index > source.end_index:
        raise RecordUsageError(
            f"Replay range C{start.index}-C{end_index} is outside the manifest "
            f"analysis range C{source.start_index}-C{source.end_index}"
        )
    try:
        full = pin_source_selection(
            store, f"C{source.start_index}", f"C{source.end_index}", run_id=start.run_id
        )
    except ValueError as error:
        raise RecordUsageError(f"manifest analysis range cannot be pinned: {error}") from error
    provenance = full.provenance()
    if (
        provenance["selection_sha256"] != source.selection_sha256
        or provenance["digest_scheme"] != source.digest_scheme
    ):
        raise RecordUsageError(
            "service manifest source digest does not match the complete analysis range; "
            "the Record was moved or changed, or the manifest belongs to another source"
        )


def _validate_manifest_evidence(store: RecordStore, manifest: ServiceManifest) -> None:
    source = manifest.source
    nodes = {
        node.index: node
        for node in store.list(Node, run_id=source.run_id)
        if source.start_index <= node.index <= source.end_index
    }
    refs = []
    for service in manifest.services:
        refs.extend((service.evidence.start, service.evidence.probe, service.evidence.stop))
    for unknown in manifest.unknowns:
        refs.append(unknown.evidence)
    for ref in refs:
        if ref is None:
            continue
        node = nodes.get(ref.index)
        if node is None or ref.execution_id not in node.tool_execution_ids:
            raise RecordUsageError(
                f"manifest evidence C{ref.index}/{ref.execution_id} does not identify a "
                "tool execution in the analyzed source range"
            )


def resolve_services(
    service_input: ServiceInput | None, store: RecordStore, start: Node,
    *, end_index: int | None = None,
) -> ServicesConfig | None:
    """Validate a source range and select services at its saved starting node."""
    if service_input is None:
        return None
    if service_input.static is not None:
        return service_input.static
    manifest = service_input.manifest
    assert manifest is not None
    with store.snapshot():
        _validate_manifest_source(
            store, start, start.index if end_index is None else end_index, manifest,
        )
        _validate_manifest_evidence(store, manifest)
    selected = select_services(manifest, start.index)
    services = tuple(
        ServiceSpec(
            name=service.name,
            start_command=service.launch.start_command,
            readiness_command=service.launch.readiness_command,
            cwd=service.launch.cwd,
            env=service.launch.env,
            timeout_sec=service.launch.timeout_sec,
        )
        for service in selected
    )
    return ServicesConfig(services=services)
