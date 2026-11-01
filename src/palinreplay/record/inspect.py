"""Read-only Record inspection and portable evidence export; no Agent SDK imports."""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime
from pathlib import Path

from palinreplay.core.errors import RecordUsageError
from palinreplay.core.models import (
    Execution,
    Failure,
    Node,
    Operation,
    Run,
    RunEnd,
    State,
    StateCapture,
    Step,
    ToolGroup,
)
from palinreplay.record.calls import read_call
from palinreplay.storage.codec import JSON_TYPES
from palinreplay.storage.io import export_directory, outside_store, write_json
from palinreplay.storage.store import MODELS, IntegrityError, RecordStore, artifact_refs


def payload(store, ref):
    """Lossless JSON/text/base64 view, retaining the original artifact identity."""
    if ref is None:
        return None
    value = {"ref": ref.model_dump(mode="json")}
    if ref.media_type in JSON_TYPES:
        return value | {"encoding": "json", "value": store.read_json(ref)}
    data = store.read_bytes(ref)
    try:
        return value | {"encoding": "utf-8", "value": data.decode("utf-8")}
    except UnicodeError:
        return value | {"encoding": "base64", "value": base64.b64encode(data).decode("ascii")}


def call_document(store: RecordStore, execution_id: str, *, contents: bool = True) -> dict:
    trace = read_call(store, execution_id)
    document = {
        "operation": trace.operation.model_dump(mode="json"),
        "execution": trace.execution.model_dump(mode="json"),
        "phase": trace.phase,
        "events": [event.model_dump(mode="json") for event in trace.events],
        "evidence": [item.model_dump(mode="json") for item in trace.evidence],
    }
    if contents:
        document["request"] = payload(store, trace.execution.request)
        document["context"] = payload(store, trace.execution.context)
        for view, event in zip(document["events"], trace.events, strict=True):
            view["content"] = payload(store, event.payload)
            view["detail_content"] = payload(store, event.details)
        for view, item in zip(document["evidence"], trace.evidence, strict=True):
            view["content"] = payload(store, item.payload)
        document["failures"] = [
            item.model_dump(mode="json") | {"detail_content": payload(store, item.details)}
            for item in trace.failures
        ]
    return document


def resolve_node(store: RecordStore, selector: str, run_id: str | None = None) -> Node:
    if selector.startswith("C") and selector[1:].isdigit():
        candidates = store.find_node_ids(index=int(selector[1:]), run_id=run_id)
    else:
        candidates = store.find_node_ids(node_id=selector, run_id=run_id)
    if not candidates:
        scope = f" in run {run_id!r}" if run_id is not None else ""
        raise RecordUsageError(
            f"node selector {selector!r} matches no committed node{scope}; expected exactly one"
        )
    if len(candidates) > 1:
        raise RecordUsageError(
            f"node selector {selector!r} matches {len(candidates)} committed nodes; "
            "expected exactly one; use --run or its full ID"
        )
    return store.get(Node, candidates[0])


def node_document(store: RecordStore, node: Node, *, include_calls: bool = True) -> dict:
    document = node.model_dump(mode="json") | {
        "label": f"C{node.index}",
        "context_content": payload(store, node.context),
        "input_position_content": payload(store, node.input_position),
        "state": store.get(State, node.state_id).model_dump(mode="json"),
    }
    if include_calls:
        document["calls"] = [
            call_document(store, execution)
            for execution in (node.model_execution_id, *node.tool_execution_ids)
            if execution is not None
        ]
    return document


def storage_metrics(store: RecordStore) -> dict:
    refs = {
        ref.sha256: ref
        for model in MODELS.values()
        for record in store.list(model)
        for ref in artifact_refs(record)
    }
    # Filesystem sizes are logical bytes, not allocated blocks or device IO.
    files = [p for p in store.root.rglob("*") if p.is_file() and not p.is_symlink()]
    metrics = {
        "scope": "whole-store-logical-bytes",
        "referenced_artifacts": len(refs),
        "referenced_artifact_bytes": sum(ref.size for ref in refs.values()),
        "catalog_bytes": (store.root / "catalog.sqlite").stat().st_size,
        "store_file_bytes": sum(p.stat().st_size for p in files),
        "state_count": len(store.list(State)),
        "node_count": len(store.list(Node)),
    }
    if any(state.backend == "btrfs-image.v1" for state in store.list(State)):
        metrics["external_filesystem_storage"] = (
            "Btrfs pool is excluded from store_file_bytes; StateCapture.storage reports its usage"
        )
    return metrics


def _run_document(store: RecordStore, run: Run, *, contents: bool) -> dict:
    key = {"run_id": run.run_id}
    nodes, steps = store.list(Node, **key), store.list(Step, **key)
    committed = {node.step_id for node in nodes}
    ends = store.list(RunEnd, **key)
    calls = [
        call_document(store, item.execution_id, contents=contents)
        for item in store.list(Execution, **key)
    ]
    tail = [step.step_id for step in steps if step.step_id not in committed]
    uncertain = [c["execution"]["execution_id"] for c in calls if c["phase"] == "unknown"]
    status = ends[-1].status if ends else "unfinalized"
    failures = store.list(Failure, **key)
    captures = store.list(StateCapture, **key)
    details = [store.read_json(item.details) for item in captures]
    operations, groups = store.list(Operation, **key), store.list(ToolGroup, **key)
    step_views = []
    for step in steps:
        group = next((g for g in groups if g.step_id == step.step_id), None)
        observed = {
            op.tool_call_id for op in operations if op.step_id == step.step_id and op.kind == "tool"
        }
        step_views.append(
            step.model_dump(mode="json")
            | {
                "committed": step.step_id in committed,
                "declared_tool_call_ids": list(group.tool_call_ids) if group else None,
                "unprepared_tool_call_ids": [
                    key for key in group.tool_call_ids if key not in observed
                ]
                if group
                else [],
            }
        )
    document = {
        "run": run.model_dump(mode="json"),
        "status": status,
        "record_complete": bool(nodes)
        and status in ("completed", "paused")
        and not tail
        and not uncertain,
        "task_success": "not_evaluated",
        "incomplete_steps": tail,
        "unknown_executions": uncertain,
        "pending_executions": [
            c["execution"]["execution_id"]
            for c in calls
            if c["phase"] in ("prepared", "dispatched", "unknown")
        ],
        "steps": step_views,
        "operations": [op.model_dump(mode="json") for op in operations],
        "tool_groups": [group.model_dump(mode="json") for group in groups],
        "nodes": [
            node_document(store, node, include_calls=False)
            if contents
            else node.model_dump(mode="json") | {"label": f"C{node.index}"}
            for node in nodes
        ],
        "calls": calls,
        "failures": [
            f.model_dump(mode="json") | {"detail_content": payload(store, f.details)}
            for f in failures
        ],
        "ends": [
            e.model_dump(mode="json") | {"detail_content": payload(store, e.details)} for e in ends
        ],
        "captures": [
            c.model_dump(mode="json") | {"detail_content": detail}
            for c, detail in zip(captures, details, strict=True)
        ],
        "capture_metrics": {
            "capture_count": len(captures),
            "duration_sec": sum(d.get("duration_sec", 0) for d in details),
            "archive_bytes_scanned": sum(d.get("archive_bytes_scanned", 0) for d in details),
            "archive_bytes_saved": sum(d.get("archive_bytes_saved", 0) for d in details),
            "reused_states": sum(bool(d.get("reused_state")) for d in details),
        },
    }
    if contents:
        document["config"] = payload(store, run.config)
    return document


def inspect_record(
    source: str | Path,
    *,
    run_id: str | None = None,
    node: str | None = None,
    execution: str | None = None,
    check: bool = False,
) -> dict:
    """Inspect facts; --check verifies local bytes and external Btrfs snapshot identities."""
    if node and execution:
        raise RecordUsageError("select a node or an execution, not both")
    with RecordStore.open(source) as store, store.snapshot():
        verification = {"checked": False, "ok": None}
        if check:
            started = time.monotonic()
            report = store.check_integrity()
            verification = report.model_dump(mode="json") | {
                "checked": True,
                "ok": report.ok,
                "duration_sec": time.monotonic() - started,
            }
            if any(state.backend == "btrfs-image.v1" for state in store.list(State)):
                verification.update(
                    scope="artifacts-and-btrfs-snapshot-identities",
                    external_artifacts="Btrfs data checksums on read; full files via verify-restore",
                )
            if not report.ok:
                return {"verification": verification, "runs": []}
        runs = store.list(Run)
        if run_id:
            runs = tuple(run for run in runs if run.run_id == run_id)
            if not runs:
                raise RecordUsageError("unknown run ID")
        document = {
            "schema_version": "palinreplay.inspect.v1",
            "verification": verification,
            "runs": [_run_document(store, run, contents=False) for run in runs],
            "storage": storage_metrics(store),
            "states": [s.model_dump(mode="json") for s in store.list(State)],
        }
        if node:
            document["selected_node"] = node_document(store, resolve_node(store, node, run_id))
        if execution:
            selected = call_document(store, execution)
            if run_id and selected["execution"]["run_id"] != run_id:
                raise RecordUsageError("execution belongs to a different run")
            document["selected_execution"] = selected
        return document


def export_record(source: str | Path, output: str | Path, *, bundle: bool = False) -> dict:
    """Export evidence JSON, optionally plus a self-contained reopenable Record store.

    A trajectory is a viewing document, not a restorable store. Bundle includes
    ALL referenced raw bytes (including captured files); never hardlink to source.
    Corruption refuses publication. Intact failed/incomplete histories are valid exports.
    """
    source, output = Path(source).absolute(), Path(output).absolute()
    outside_store(source, output)
    started = time.monotonic()
    with export_directory(output) as stage, RecordStore.open(source) as store, store.snapshot():
        report = store.check_integrity()
        if not report.ok:
            raise IntegrityError(f"export refused: {len(report.issues)} integrity issue(s)")
        records = {name: store.list(model) for name, model in MODELS.items()}
        if bundle:
            if any(state.backend == "btrfs-image.v1" for state in records["state"]):
                raise RecordUsageError("portable bundles for external Btrfs pools are not supported")
        trajectory = {
            "schema_version": "palinreplay.trajectory.v1",
            "restorable": False,
            "filesystem_payloads": "omitted; restore from the source record and its Btrfs pool",
            "verification": report.model_dump(mode="json") | {"ok": report.ok},
            "runs": [_run_document(store, run, contents=True) for run in records["run"]],
            # Preserve every identity, including orphaned captures and unfinished attempts.
            "facts": {
                name: [record.model_dump(mode="json") for record in facts]
                for name, facts in records.items()
            },
            "storage": storage_metrics(store),
        }
        write_json(stage / "trajectory.json", trajectory)
        refs = {
            ref.sha256: ref
            for facts in records.values()
            for record in facts
            for ref in artifact_refs(record)
        }
        copied = 0
        if bundle:
            target = stage / "record"
            (target / "artifacts" / ".tmp").mkdir(mode=0o700, parents=True)
            target.chmod(0o700)
            (target / "artifacts").chmod(0o700)
            store.export_catalog(target / "catalog.sqlite")
            for ref in refs.values():
                shard = target / "artifacts" / ref.sha256[:2]
                shard.mkdir(mode=0o700, exist_ok=True)
                store.export_artifact(ref, shard / ref.sha256[2:])
                copied += ref.size
        manifest = {
            "schema_version": "palinreplay.export.v1",
            "created_at": datetime.now(UTC).isoformat(),
            "kind": "bundle" if bundle else "trajectory",
            "store_path": "record" if bundle else None,
            "source_integrity_ok": True,
            "incomplete_steps": list(report.incomplete_steps),
            "pending_executions": list(report.pending_executions),
            "omitted_staged_artifacts": list(report.staged_artifacts),
            "artifact_bytes_copied": copied,
            "duration_sec": time.monotonic() - started,
        }
        write_json(stage / "export.json", manifest)
    return manifest
