"""Restore a recorded node, verify its files and own verification resources."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from uuid import uuid4

import palinreplay.storage.rootfs as _rootfs
from palinreplay.core.models import Node, State
from palinreplay.core.serialization import json_bytes
from palinreplay.sandbox.base import SandboxCleanupError, SandboxError
from palinreplay.sandbox.btrfs import BtrfsWorkspace
from palinreplay.storage.artifacts import sync_directory
from palinreplay.storage.btrfs.models import BACKEND, BtrfsRuntime, Snapshot
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.btrfs.verification import capture_info
from palinreplay.storage.store import IntegrityError, RecordStore


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
        if self._closed or self.report["status"] != "verified":
            raise SandboxError("restored node is not available")
        return self

    def __exit__(self, exc_type, error, traceback) -> None:
        try:
            self.close()
        except BaseException as cleanup:
            if error is None:
                raise
            error.add_note(f"restore cleanup also failed: {cleanup!r}")


def restore_node(
    store: RecordStore,
    node_id: str,
    *,
    client,
    docker_host: str,
    report_path: str | Path,
) -> RestoredNode:
    """Restore a committed Btrfs node and fully compare its saved file state."""
    if not docker_host.startswith("unix:///"):
        raise ValueError("restoration supports an explicit local Unix Docker endpoint")
    report_path = Path(report_path).absolute()
    if report_path.resolve().is_relative_to(store.root.resolve()):
        raise ValueError("restore report must be outside the immutable source store")

    node = store.get(Node, node_id)
    state = store.get(State, node.state_id)
    if state.backend != BACKEND:
        raise IntegrityError("only Btrfs file restoration is supported")
    return restore_btrfs_node(store, node_id, client=client, report_path=report_path)

def restore_btrfs_node(store, node_id, *, client, report_path):

    restored = RestoredNode(client, node_id, report_path)
    restored.report["backend"] = BACKEND
    try:
        node = store.get(Node, node_id)
        state = store.get(State, node.state_id)
        if state.backend != BACKEND or state.capture != capture_info() or state.dependencies:
            raise IntegrityError("unsupported Btrfs capture scope or dependencies")
        snapshot = Snapshot.model_validate(store.read_json(state.manifest))
        runtime = BtrfsRuntime.model_validate(store.read_json(state.runtime))
        restored.pool = BtrfsPool(client, snapshot.location)
        restored.pool.__enter__()
        store.get_node(node_id)  # Validates graph, immutable artifacts and snapshot identity.
        restored.workspace = BtrfsWorkspace(restored.pool, snapshot, restored._owner)
        restored.workspace.start(
            runtime.launch,
            runtime.bootstrap_image_id,
            environment=runtime.launch.environment,
            labels={},
        )
        restored.container = restored.workspace.container
        restored.node, restored.state = node, state
        restored.report.update(
            container_name=restored.workspace.container_name,
            source_container_id=runtime.source_container_id,
            source_image_id=runtime.source_image_id,
            bootstrap_image_id=runtime.bootstrap_image_id,
            state_id=state.state_id,
            phase="verify_files",
        )
        restored._write_report()
        comparison = restored.pool.command(
            "verify",
            source=snapshot.name,
            workspace=restored.workspace.name,
            scope=_rootfs.SCOPE,
            oracle_sha256=runtime.c0_oracle_sha256,
        )
        restored.report.update(
            status="verified",
            phase="complete",
            material_verified=True,
            file_comparison=comparison,
            verification="full-logical-filesystem",
        )
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
