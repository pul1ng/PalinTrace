"""Versioned references to Btrfs pools, snapshots and launch materials."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field

from palinreplay.core.schema import Spec
from palinreplay.sandbox.runtime import C0LaunchConfig

BACKEND = "btrfs-image.v1"


class PoolLocation(Spec):
    volume: str = Field(pattern=r"^palinreplay-btrfs-[0-9a-f]{32}$")
    uuid: UUID
    daemon_id: str = Field(min_length=1)
    helper_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class Snapshot(Spec):
    schema_version: Literal["palinreplay.btrfs-snapshot.v1"] = "palinreplay.btrfs-snapshot.v1"
    location: PoolLocation
    name: str = Field(pattern=r"^(bases/C0|checkpoints/[0-9a-f]{32})$")
    uuid: UUID


class BtrfsC0(Spec):
    schema_version: Literal["palinreplay.btrfs-preload.v1"] = "palinreplay.btrfs-preload.v1"
    backend: Literal["btrfs-image.v1"] = BACKEND
    task_id: str | None
    source_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initialization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    oracle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bootstrap_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    snapshot: Snapshot
    launch: C0LaunchConfig


class BtrfsRuntime(Spec):
    schema_version: Literal["palinreplay.btrfs-runtime.v1"] = "palinreplay.btrfs-runtime.v1"
    source_container_id: str
    source_image_id: str
    bootstrap_image_id: str
    launch: C0LaunchConfig
    # Used only by explicit C0 restore verification, not ordinary attachment.
    c0_oracle_sha256: str


@dataclass(frozen=True)
class FrozenBtrfs:
    path: Path
    metadata: BtrfsC0
    metadata_sha256: str
