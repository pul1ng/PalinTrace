"""Restore a recorded node, verify its files and own verification resources."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

import palinreplay.storage.rootfs as _rootfs
from palinreplay.core.models import Node, Run, State
from palinreplay.core.serialization import json_bytes
from palinreplay.record.inspect import resolve_node
from palinreplay.sandbox.base import SandboxCleanupError, SandboxError
from palinreplay.sandbox.btrfs import BtrfsWorkspace
from palinreplay.storage.artifacts import sync_directory
from palinreplay.storage.btrfs.models import BACKEND, BtrfsRuntime, Snapshot
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.btrfs.verification import capture_info
from palinreplay.storage.store import IntegrityError, RecordStore


@dataclass(frozen=True)
class RestoreMaterials:
    """Saved node materials; external snapshot identity and files are not verified."""

    run: Run
    node: Node
    state: State
    snapshot: Snapshot
    runtime: BtrfsRuntime
    config: object
    context: object
    input_position: object


def load_restore_materials(
    store: RecordStore, selector: str, *, run_id: str | None = None
) -> RestoreMaterials:
    """Read one committed node's required materials without creating restore resources."""
    with store.snapshot():
        node = resolve_node(store, selector, run_id)
        run = store.get(Run, node.run_id)
        state = store.get(State, node.state_id)
        if state.backend != BACKEND or state.capture != capture_info() or state.dependencies:
            raise IntegrityError("unsupported Btrfs capture scope or dependencies")

        def read(role, ref):
            try:
                return store.read_json(ref)
            except IntegrityError as error:
                raise IntegrityError(f"invalid {role} material: {error}") from error

        config = read("run config", run.config)
        context = read("node context", node.context)
        input_position = read("node input position", node.input_position)
        try:
            snapshot = Snapshot.model_validate(read("snapshot manifest", state.manifest))
        except ValidationError as error:
            raise IntegrityError("invalid snapshot manifest material") from error
        try:
            runtime = BtrfsRuntime.model_validate(read("Btrfs runtime", state.runtime))
        except ValidationError as error:
            raise IntegrityError("invalid Btrfs runtime material") from error

        read_refs = (run.config, node.context, node.input_position, state.manifest, state.runtime)
        for ref in state.artifacts:
            if ref not in read_refs:
                try:
                    store.verify_artifact(ref)
                except IntegrityError as error:
                    raise IntegrityError(f"invalid state artifact {ref.sha256}: {error}") from error

        return RestoreMaterials(run, node, state, snapshot, runtime, config, context, input_position)


class RestoredNode:
    """Independent idle Btrfs container with owned resources and a durable report."""

    def __init__(self, client, node_id: str, report_path: Path):

        self.client = client
        self.container = None
        self.node: Node | None = None
        self.state: State | None = None
        self.pool = None
        self.workspace = None
        self._owner = uuid4().hex
        self._closed = False
        self.report_path = report_path.absolute()
        self.report = {
            "schema_version": "palinreplay.file-restore.v1",
            "node_id": node_id,
            "status": "in_progress",
            "phase": "validate_material",
            "owner": self._owner,
            "cleanup_confirmed": False,
            "cleanup_errors": [],
            "limitations": list(capture_info().limitations),
        }
        with self.report_path.open("xb") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(json_bytes(self.report))
            target.flush()
            os.fsync(target.fileno())
        sync_directory(self.report_path.parent)

    def _write_report(self) -> None:
        fd, name = tempfile.mkstemp(prefix=".restore-report-", dir=self.report_path.parent)
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(json_bytes(self.report))
                target.flush()
                os.fsync(target.fileno())
            os.replace(name, self.report_path)
            sync_directory(self.report_path.parent)
        finally:
            Path(name).unlink(missing_ok=True)

    def close(self):
        if self._closed:
            return
        errors = []
        for name, resource in [("workspace", self.workspace), ("pool", self.pool)]:
            if resource is not None:
                try:
                    resource.close()
                except Exception as error:
                    errors.append(f"{name}: {type(error).__name__}: {error}")
        self.report.update(cleanup_confirmed=not errors, cleanup_errors=errors)
        self._write_report()
        if errors:
            raise SandboxCleanupError("; ".join(errors))
        self._closed = True

    def __enter__(self) -> RestoredNode:
        if self._closed or self.report["status"] not in ("ready", "verified"):
            raise SandboxError("restored node is not available")
        return self

    def __exit__(self, exc_type, error, traceback) -> None:
        try:
            self.close()
        except BaseException as cleanup:
            if error is None:
                raise
            error.add_note(f"restore cleanup also failed: {cleanup!r}")


def _restore_report_path(store, docker_host, report_path):
    if not docker_host.startswith("unix:///"):
        raise ValueError("restoration supports an explicit local Unix Docker endpoint")
    report_path = Path(report_path).absolute()
    if report_path.resolve().is_relative_to(store.root.resolve()):
        raise ValueError("restore report must be outside the immutable source store")
    return report_path


def restore_r1_node(
    store: RecordStore,
    selector: str,
    *,
    run_id: str | None = None,
    client,
    docker_host: str,
    report_path: str | Path,
) -> RestoredNode:
    """Create an independent idle environment without a full filesystem comparison."""
    report_path = _restore_report_path(store, docker_host, report_path)
    materials = load_restore_materials(store, selector, run_id=run_id)
    return _restore_btrfs_node(
        store, materials, client=client, report_path=report_path, compare_files=False
    )


def restore_node(
    store: RecordStore,
    node_id: str,
    *,
    client,
    docker_host: str,
    report_path: str | Path,
) -> RestoredNode:
    """Restore a committed Btrfs node and fully compare its saved file state."""
    report_path = _restore_report_path(store, docker_host, report_path)
    return restore_btrfs_node(store, node_id, client=client, report_path=report_path)


def restore_btrfs_node(store, node_id, *, client, report_path):
    materials = load_restore_materials(store, node_id)
    return _restore_btrfs_node(
        store, materials, client=client, report_path=Path(report_path), compare_files=True
    )


def _start_workspace(restored: RestoredNode, materials: RestoreMaterials) -> None:
    runtime = materials.runtime
    restored.workspace = BtrfsWorkspace(restored.pool, materials.snapshot, restored._owner)
    restored.workspace.start(
        runtime.launch,
        runtime.bootstrap_image_id,
        environment=runtime.launch.environment,
        labels={},
    )
    restored.container = restored.workspace.container
    restored.node, restored.state = materials.node, materials.state
    restored.report.update(
        container_name=restored.workspace.container_name,
        source_container_id=runtime.source_container_id,
        source_image_id=runtime.source_image_id,
        bootstrap_image_id=runtime.bootstrap_image_id,
        state_id=materials.state.state_id,
    )


def _compare_files(restored: RestoredNode, materials: RestoreMaterials) -> dict:
    return restored.pool.command(
        "verify",
        source=materials.snapshot.name,
        workspace=restored.workspace.name,
        scope=_rootfs.SCOPE,
        oracle_sha256=materials.runtime.c0_oracle_sha256,
    )


def _restore_btrfs_node(store, materials, *, client, report_path, compare_files):
    restored = RestoredNode(client, materials.node.node_id, report_path)
    restored.report["backend"] = BACKEND
    try:
        restored.pool = BtrfsPool(client, materials.snapshot.location)
        restored.pool.__enter__()
        if compare_files:
            # The active session pool serves strict graph/material checks too.
            store.get_node(materials.node.node_id)
        restored.report["phase"] = "create_environment"
        _start_workspace(restored, materials)
        if compare_files:
            restored.report["phase"] = "verify_files"
            restored._write_report()
            comparison = _compare_files(restored, materials)
            restored.report.update(
                status="verified",
                material_verified=True,
                file_comparison=comparison,
                verification="full-logical-filesystem",
            )
        else:
            restored.report.update(status="ready", verification="snapshot-identity")
        restored.report["phase"] = "complete"
        restored._write_report()
        return restored
    except BaseException as error:
        restored.report.update(status="failed", error_type=type(error).__name__)
        try:
            restored.close()
        except Exception as cleanup:
            error.add_note(f"Btrfs restore cleanup unconfirmed: {cleanup}")
        error.restore_report = restored.report
        raise
