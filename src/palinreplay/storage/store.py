"""Transactional Record metadata and immutable artifacts on a local filesystem.

Objects are flushed before their references can be committed. SQLite is the
single metadata authority; unfinished facts remain inspectable without a Node.
No model calls, container operations, history replay or automatic retry occurs.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from palinreplay.core.errors import (
    ConflictError,
    IncompleteRecordError,
    IntegrityError,
    RecordUsageError,
    StoreError,
)
from palinreplay.core.interfaces import Payload
from palinreplay.core.models import (
    ArtifactRef,
    Attempt,
    CallEvidence,
    Event,
    EventKind,
    Execution,
    Fact,
    Failure,
    IntegrityIssue,
    IntegrityReport,
    Node,
    Operation,
    Outcome,
    Run,
    RunEnd,
    State,
    StateCapture,
    Step,
    StepCalls,
    ToolGroup,
)
from palinreplay.core.replay_contract import SourceReference, source_reference
from palinreplay.core.serialization import json_bytes
from palinreplay.diagnostics.metrics import CountingReader, timed
from palinreplay.diagnostics.timing import span
from palinreplay.storage.artifacts import ArtifactStore, plain_directory, sync_directory
from palinreplay.storage.payloads import PayloadStore, is_shared

__all__ = [
    "ConflictError", "IncompleteRecordError", "IntegrityError", "RecordStore", "StoreError", "store_path",
]

STORE_VERSION = "palinreplay.store.v2"
LEGACY_STORE_VERSION = "palinreplay.store.v1"
MODELS = {
    cls.__name__.lower(): cls
    for cls in (
        Run, Attempt, Step, Operation, Execution, Event, State, Node, Failure,
        ToolGroup, RunEnd, StateCapture, CallEvidence,
    )
}
T = TypeVar("T", bound=Fact)


def store_path(path: Path) -> Path:
    if (path / "catalog.sqlite").exists():
        return path
    if (path / "record" / "catalog.sqlite").exists():
        return path / "record"
    raise RecordUsageError("expected a Record store or run/bundle directory containing record/")


def identity(record: Fact) -> str:
    return getattr(record, f"{type(record).__name__.lower()}_id")


def artifact_refs(value) -> Iterator[ArtifactRef]:
    if isinstance(value, ArtifactRef):
        yield value
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from artifact_refs(getattr(value, name))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from artifact_refs(item)


class RecordStore:
    @contextmanager
    def _external_checks(self):
        if (self.root / "btrfs.json").exists():
            from palinreplay.storage.btrfs.verification import verification_scope

            with verification_scope():
                yield
        else:
            yield

    def __init__(
        self, root: Path, connection: sqlite3.Connection, read_only: bool,
        *, payload_layout: str = "shared",
    ):
        self.root = root
        self._db = connection
        self._db.row_factory = sqlite3.Row
        self._artifacts = ArtifactStore(root / "artifacts")
        self._payloads = PayloadStore(self._artifacts)
        self.payload_layout = payload_layout
        self.read_only = read_only
        self._closed = False
        self._source_cache = None
        self._receipt_cache = None

    @classmethod
    def create(cls, path: str | Path, *, payload_layout: str = "shared") -> RecordStore:
        if payload_layout not in {"shared", "flat"}:
            raise ValueError("payload_layout must be 'shared' or 'flat'")
        root = Path(path).absolute()
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        (root / "artifacts").mkdir(mode=0o700)
        (root / "artifacts" / ".tmp").mkdir(mode=0o700)
        sync_directory(root / "artifacts")
        db = sqlite3.connect(root / "catalog.sqlite", isolation_level=None)
        try:
            db.execute("PRAGMA synchronous = FULL")
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE records (
                    kind TEXT NOT NULL, id TEXT NOT NULL, run_id TEXT,
                    ordinal INTEGER, body TEXT NOT NULL, sha256 TEXT NOT NULL,
                    PRIMARY KEY (kind, id)
                );
                CREATE INDEX records_run ON records (kind, run_id);
                CREATE UNIQUE INDEX event_order ON records (run_id, ordinal)
                    WHERE kind = 'event';
                CREATE UNIQUE INDEX node_order ON records (run_id, ordinal)
                    WHERE kind = 'node';
                COMMIT;
            """)
            version = STORE_VERSION if payload_layout == "shared" else LEGACY_STORE_VERSION
            db.execute("INSERT INTO metadata VALUES ('schema_version', ?)", (version,))
            sync_directory(root)
            sync_directory(root.parent)
        except BaseException:
            db.close()
            raise
        return cls(root, db, False, payload_layout=payload_layout)

    @classmethod
    def open(cls, path: str | Path, *, read_only: bool = True) -> RecordStore:
        root = Path(path).absolute()
        plain_directory(root)
        plain_directory(root / "artifacts")
        plain_directory(root / "artifacts" / ".tmp")
        catalog = root / "catalog.sqlite"
        if catalog.is_symlink() or not catalog.is_file():
            raise IntegrityError("missing catalog or unexpected catalog symlink")
        mode = "ro" if read_only else "rw"
        db = sqlite3.connect(catalog.as_uri() + f"?mode={mode}", uri=True, isolation_level=None)
        try:
            version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if version is None or version[0] not in {STORE_VERSION, LEGACY_STORE_VERSION}:
                raise IntegrityError("unsupported Record store schema version")
            db.execute("PRAGMA synchronous = FULL")
            db.execute("SELECT kind, id, run_id, ordinal, body, sha256 FROM records LIMIT 0")
            layout = "shared" if version[0] == STORE_VERSION else "flat"
            store = cls(root, db, read_only, payload_layout=layout)
        except (sqlite3.DatabaseError, IntegrityError) as exc:
            db.close()
            raise IntegrityError(f"cannot open Record catalog: {exc}") from exc
        except BaseException:
            db.close()
            raise
        return store

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    def __enter__(self) -> RecordStore:
        self._ensure_open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _ensure_open(self, *, writing: bool = False) -> None:
        if self._closed:
            raise StoreError("Record store is closed")
        if writing and self.read_only:
            raise StoreError("Record store is read-only")

    @contextmanager
    def _transaction(self):
        self._ensure_open(writing=True)
        if self._source_cache is not None:
            self._source_cache.clear()
            self._receipt_cache.clear()
        with span("catalog.begin"):
            self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            with span("catalog.commit"):
                self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                with span("catalog.rollback"):
                    self._db.execute("ROLLBACK")
            raise

    @contextmanager
    def _snapshot(self):
        self._ensure_open()
        nested = self._db.in_transaction
        owner = self._source_cache is None
        if owner:
            self._source_cache, self._receipt_cache = {}, {}
        try:
            if not nested:
                self._db.execute("BEGIN")
            try:
                with self._payloads.scope():
                    yield
            finally:
                if not nested:
                    self._db.execute("ROLLBACK")
        finally:
            if owner:
                self._source_cache = self._receipt_cache = None

    @contextmanager
    def snapshot(self):
        """Hold one catalog read boundary for inspection or portable export.

        Artifacts are immutable. Readers must keep this boundary while choosing
        facts and copying their bytes, so a concurrent writer cannot mix steps.
        """
        with self._snapshot():
            # Establish SQLite's read view before any backup or filesystem IO.
            self._db.execute("SELECT COUNT(*) FROM records").fetchone()
            yield self

    def export_catalog(self, destination: str | Path) -> None:
        """Copy a consistent SQLite catalog, never a live database file/WAL."""
        self._ensure_open()
        destination = Path(destination)
        with destination.open("xb"):
            pass
        try:
            with closing(sqlite3.connect(destination)) as target:
                with self.snapshot():
                    self._db.backup(target)
            destination.chmod(0o600)
            with destination.open("rb") as handle:
                os.fsync(handle.fileno())
            sync_directory(destination.parent)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    def put_bytes(
        self, data: bytes, *, media_type: str = "application/octet-stream"
    ) -> ArtifactRef:
        self._ensure_open(writing=True)
        return self._artifacts.put(data, media_type)

    def put_json(self, value, *, compress=False) -> ArtifactRef:
        return self._put_json_with_size(value, compress=compress)[0]

    def put_payload(self, payload: Payload) -> ArtifactRef:
        """Persist exact captured bytes, sharing canonical JSON in new stores."""
        self._ensure_open(writing=True)
        if not isinstance(payload, Payload):
            raise TypeError("captured payload must be a Payload")
        if self.payload_layout == "flat":
            return self.put_bytes(payload.data, media_type=payload.media_type)
        return self._payloads.put(payload)

    def put_payload_json(self, value) -> ArtifactRef:
        return self.put_payload(Payload.json(value))

    def _put_json_with_size(self, value, *, compress=False) -> tuple[ArtifactRef, int]:
        """Publish JSON once and return its exact pre-compression byte length."""
        import palinreplay.storage.codec as _json_codec

        data, media = json_bytes(value), "application/json"
        logical_size = len(data)
        if compress:
            data, media = _json_codec.compress(data)
        return self.put_bytes(data, media_type=media), logical_size

    def put_file(
        self, path: str | Path, *, media_type: str = "application/octet-stream"
    ) -> ArtifactRef:
        self._ensure_open(writing=True)
        with Path(path).open("rb") as handle:
            return self._artifacts.put_stream(CountingReader(handle, copying=True), media_type)

    @contextmanager
    def stage_file(self):
        """Create an owned path for one closed-producer, immutable artifact transfer."""
        self._ensure_open(writing=True)
        with self._artifacts.stage_file() as path:
            yield path

    def publish_staged_file(self, path, *, media_type="application/octet-stream") -> ArtifactRef:
        self._ensure_open(writing=True)
        return self._artifacts.publish_staged_file(path, media_type)

    def _publish_inventoried_file(self, path, inventory, *, media_type) -> ArtifactRef:
        """Internal full-stream path; the receipt never substitutes for a byte check."""
        self._ensure_open(writing=True)
        inventory.check_path(path)
        if inventory.sha256 is None:
            raise IntegrityError("full-stream publication requires an archive digest")
        expected = ArtifactRef(sha256=inventory.sha256, size=inventory.size, media_type=media_type)
        with self._artifacts._expect_stream(path, expected, inventory.identity):
            ref = self.publish_staged_file(path, media_type=media_type)
        inventory.check_artifact(ref)
        return ref

    @contextmanager
    def _expansion_scope(self):
        """Reuse logical expansions across one complete node operation."""
        with self._payloads.expansion_scope():
            yield

    @contextmanager
    def _verification_scope(self):
        self._ensure_open(writing=True)
        with (
            self._artifacts.verification_scope(), self._payloads.scope(),
            self._payloads.expansion_scope(),
        ):
            yield

    def _verify_artifact(self, ref):
        self._ensure_open()
        if is_shared(ref.media_type):
            self._payloads.read(ref)
        else:
            self._artifacts.verify_internal(ref)

    def read_bytes(self, ref: ArtifactRef) -> bytes:
        self._ensure_open()
        return self._artifacts.read(ref)

    def read_payload(self, ref: ArtifactRef) -> Payload:
        """Expand shared payloads; raw artifacts retain their exact bytes/media."""
        self._ensure_open()
        return self._payloads.read(ref, data=self.read_bytes(ref))

    def iter_payload_artifacts(self, ref: ArtifactRef) -> Iterator[ArtifactRef]:
        """Yield distinct physical objects in a verified shared payload closure."""
        self._ensure_open()
        yield from self._payloads.iter_artifacts(ref)

    def export_artifact(self, ref: ArtifactRef, destination: str | Path) -> None:
        """Copy verified bytes to a new file, including when this store is read-only."""
        self._ensure_open()
        self._artifacts.copy_to(ref, Path(destination))

    def read_json(self, ref: ArtifactRef):
        from palinreplay.storage.codec import decode

        try:
            payload = self.read_payload(ref)
            return json.loads(decode(payload.data, payload.media_type))
        except (ValueError, UnicodeError) as exc:
            raise IntegrityError(f"artifact is not valid JSON: {ref.sha256}") from exc

    def verify_artifact(self, ref: ArtifactRef) -> None:
        self._ensure_open()
        if is_shared(ref.media_type):
            self._payloads.read(ref)
        else:
            self._artifacts.verify(ref)

    def _decode(self, row: sqlite3.Row) -> Fact:
        if not isinstance(row["body"], str) or not isinstance(row["sha256"], str):
            raise IntegrityError(f"invalid metadata encoding: {row['kind']}/{row['id']}")
        data = row["body"].encode("utf-8")
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise IntegrityError(f"metadata hash mismatch: {row['kind']}/{row['id']}")
        try:
            model = MODELS[row["kind"]]
            value = model.model_validate_json(data)
        except (KeyError, ValidationError) as exc:
            raise IntegrityError(
                f"invalid/version-incompatible metadata: {row['kind']}/{row['id']}"
            ) from exc
        ordinal = (
            value.sequence
            if isinstance(value, Event)
            else value.index
            if isinstance(value, Node)
            else None
        )
        if (
            identity(value) != row["id"]
            or getattr(value, "run_id", None) != row["run_id"]
            or ordinal != row["ordinal"]
        ):
            raise IntegrityError(f"catalog identity mismatch: {row['kind']}/{row['id']}")
        return value

    def get(self, model: type[T], record_id: str) -> T:
        """Read metadata with hash/schema validation; use get_node for material validation."""
        self._ensure_open()
        row = self._db.execute(
            "SELECT * FROM records WHERE kind=? AND id=?", (model.__name__.lower(), record_id)
        ).fetchone()
        if row is None:
            raise IntegrityError(f"missing record: {model.__name__}/{record_id}")
        return self._decode(row)

    def list(self, model: type[T], *, run_id: str | None = None) -> tuple[T, ...]:
        """List metadata only, without scanning large state artifacts."""
        self._ensure_open()
        sql, params = "SELECT * FROM records WHERE kind=?", [model.__name__.lower()]
        if run_id is not None:
            sql += " AND run_id=?"
            params.append(run_id)
        return tuple(self._decode(row) for row in self._db.execute(sql + " ORDER BY rowid", params))

    def find_node_ids(
        self, *, node_id: str | None = None, index: int | None = None, run_id: str | None = None
    ) -> tuple[str, ...]:
        """Locate candidates in the catalog; get() validates the selected node's metadata."""
        self._ensure_open()
        sql, params = "SELECT id FROM records WHERE kind='node'", []
        for column, value in (("id", node_id), ("ordinal", index), ("run_id", run_id)):
            if value is not None:
                sql += f" AND {column}=?"
                params.append(value)
        return tuple(row[0] for row in self._db.execute(sql + " ORDER BY rowid", params))

    def _insert(self, record: Fact) -> None:
        body = json_bytes(record.model_dump(mode="json"))
        ordinal = (
            record.sequence
            if isinstance(record, Event)
            else record.index
            if isinstance(record, Node)
            else None
        )
        try:
            self._db.execute(
                "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?)",
                (
                    type(record).__name__.lower(),
                    identity(record),
                    getattr(record, "run_id", None),
                    ordinal,
                    body.decode("utf-8"),
                    hashlib.sha256(body).hexdigest(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"identity or sequence already exists: {identity(record)}") from exc

    def _existing(self, record: T) -> T | None:
        row = self._db.execute(
            "SELECT * FROM records WHERE kind=? AND id=?",
            (type(record).__name__.lower(), identity(record)),
        ).fetchone()
        if row is None:
            return None
        existing = self._decode(row)
        if existing != record:
            raise ConflictError(f"cannot overwrite immutable record: {identity(record)}")
        return existing

    @timed("store.verify_refs")
    def _verify_refs(self, records, seen: set | None = None) -> None:
        seen = seen if seen is not None else set()
        for record in records:
            for ref in artifact_refs(record):
                key = (ref.sha256, ref.size, ref.media_type)
                if key not in seen:
                    self._verify_artifact(ref)
                    seen.add(key)

    def _same_run(self, run_id: str, *records: Fact) -> None:
        if any(getattr(item, "run_id", None) != run_id for item in records):
            raise IntegrityError("cross-run reference")

    def _step_open(self, step_id: str) -> None:
        self._attempt_open(self.get(Step, step_id).attempt_id)
        if any(node.step_id == step_id for node in self.list(Node)):
            raise ConflictError("cannot append calls/events to a committed step")

    def _attempt_open(self, attempt_id: str) -> None:
        if any(end.attempt_id == attempt_id for end in self.list(RunEnd)):
            raise ConflictError("cannot append to an ended attempt")

    def _tip(self, run_id: str) -> Node | None:
        return max(self.list(Node, run_id=run_id), key=lambda n: n.index, default=None)

    def replay_source(self, run_id: str) -> SourceReference | None:
        return self._recording_source(run_id, "replay")

    def resume_source(self, run_id: str) -> SourceReference | None:
        return self._recording_source(run_id, "resume")

    def recording_source(self, run_id: str) -> SourceReference | None:
        """Read the external start for either a fixed Replay or an open-ended Resume."""
        return self._recording_source(run_id, None)

    def _recording_source(self, run_id, mode):
        """Read protected config without treating external IDs as local relations."""
        from palinreplay.core.resume_contract import resume_source_reference
        from palinreplay.storage.codec import JSON_TYPES, decode

        with self._snapshot():
            run = self.get(Run, run_id)
            key = (run_id, run.config.sha256, run.config.size, run.config.media_type)
            if key not in self._source_cache:
                source = None
                if run.config.media_type in JSON_TYPES or is_shared(run.config.media_type):
                    payload = self.read_payload(run.config)
                    data = decode(payload.data, payload.media_type)
                    try:
                        config = json.loads(data)
                    except (ValueError, UnicodeError):
                        pass  # Ordinary Record configurations may be opaque payloads.
                    else:
                        source = resume_source_reference(config) or source_reference(config)
                self._source_cache[key] = source
            source = self._source_cache[key]
            return source if source is None or mode is None or source.mode == mode else None

    def replay_finalization(self, run_id: str):
        """Freeze sidecar presence/bytes alongside one catalog read operation."""
        from palinreplay.storage.replay_finalization import read_receipt

        with self._snapshot():
            source = self.replay_source(run_id)
            if source is None:
                return None
            if source.finalization_protocol is None:
                return {"protocol": None, "status": "legacy", "receipt": None}
            if run_id not in self._receipt_cache:
                attempts = self.list(Attempt, run_id=run_id)
                receipt = None
                if attempts:
                    attempt = attempts[-1]
                    ends = [e for e in self.list(RunEnd, run_id=run_id)
                            if e.attempt_id == attempt.attempt_id]
                    failures = [f for f in self.list(Failure, run_id=run_id)
                                if f.attempt_id == attempt.attempt_id]
                    receipt = read_receipt(self.root, self.get(Run, run_id), attempt,
                                           ends[-1] if ends else None, failures)
                    if receipt is not None:
                        from palinreplay.core.replay_outcome import (
                            execution_status,
                            failure_evidence,
                        )

                        status = ends[-1].status if ends else "unfinalized"
                        if ends:
                            saved = self.read_json(ends[-1].details).get("replay", {})
                            status = saved.get("execution_status", status)
                        for failure in failures:
                            status = execution_status(status, **failure_evidence(
                                failure.category, self.read_json(failure.details)))
                        if ((status == "failed" and receipt["execution_status"] != "failed")
                                or (status == "stopped" and receipt["execution_status"] == "completed")
                                or (status == "cancelled" and receipt["execution_status"] not in ("cancelled", "failed"))
                                or (receipt["execution_status"] == "completed"
                                    and len(self.list(Node, run_id=run_id)) != source.end.index - source.start.index)):
                            raise IntegrityError("Replay receipt suppresses persisted execution evidence")
                self._receipt_cache[run_id] = {
                    "protocol": source.finalization_protocol,
                    "status": "confirmed" if receipt is not None else "unconfirmed",
                    "receipt": receipt,
                }
            return self._receipt_cache[run_id]

    def _relations(self, record: Fact) -> None:
        """Validate durable relations, also usable when inspecting old history."""
        if not isinstance(record, State):
            if not isinstance(record, Run):
                self.get(Run, record.run_id)
        if isinstance(record, Run) and record.source_node_id:
            source = self.get(Node, record.source_node_id)
            if source.run_id == record.run_id:
                raise IntegrityError("run source must be a node from a different run")
        elif isinstance(record, Attempt):
            if record.previous_attempt_id:
                previous = self.get(Attempt, record.previous_attempt_id)
                self._same_run(record.run_id, previous)
                if previous.attempt_id == record.attempt_id:
                    raise IntegrityError("attempt cannot precede itself")
        elif isinstance(record, Step):
            attempt = self.get(Attempt, record.attempt_id)
            self._same_run(record.run_id, attempt)
            source = self.recording_source(record.run_id)
            if (source is not None and source.mode == "replay"
                    and not source.start.index < record.index <= source.end.index):
                raise IntegrityError("step index is outside the pinned Replay range")
            if record.parent_node_id is None:
                if source is None or record.index != source.start.index + 1:
                    raise IntegrityError("parentless step requires the first external source index")
            else:
                parent = self.get(Node, record.parent_node_id)
                self._same_run(record.run_id, parent)
                if record.index != parent.index + 1:
                    raise IntegrityError("step index must follow its parent node")
        elif isinstance(record, Operation):
            step = self.get(Step, record.step_id)
            self._same_run(record.run_id, step)
            if record.caused_by_execution_id:
                cause = self.get(Execution, record.caused_by_execution_id)
                cause_op = self.get(Operation, cause.operation_id)
                self._same_run(record.run_id, cause, cause_op)
                if cause_op.step_id != record.step_id:
                    raise IntegrityError("call cause belongs to another step")
        elif isinstance(record, Execution):
            self._same_run(record.run_id, self.get(Operation, record.operation_id))
        elif isinstance(record, CallEvidence):
            execution = self.get(Execution, record.execution_id)
            self._same_run(record.run_id, execution)
            evidence = [
                e for e in self.list(CallEvidence, run_id=record.run_id)
                if e.execution_id == record.execution_id
            ]
            if any(
                e.index == record.index and e.callevidence_id != record.callevidence_id
                for e in evidence
            ):
                raise IntegrityError("duplicate internal evidence index")
            previous = [e for e in evidence if e.index < record.index]
            if sorted(e.index for e in previous) != list(range(1, record.index)):
                raise IntegrityError("internal call evidence has gaps")
        elif isinstance(record, Event):
            self._same_run(record.run_id, self.get(Execution, record.execution_id))
        elif isinstance(record, ToolGroup):
            step = self.get(Step, record.step_id)
            execution = self.get(Execution, record.model_execution_id)
            operation = self.get(Operation, execution.operation_id)
            self._same_run(record.run_id, step, execution, operation)
            if operation.step_id != step.step_id or operation.kind != "model":
                raise IntegrityError("tool group must refer to this step's model response")
            history = self._execution_events(execution)
            if len(history) != 4 or history[1].outcome != "returned":
                raise IncompleteRecordError("tool group requires an accepted model response")
        elif isinstance(record, RunEnd):
            attempt = self.get(Attempt, record.attempt_id)
            self._same_run(record.run_id, attempt)
            if record.last_node_id:
                node = self.get(Node, record.last_node_id)
                self._same_run(record.run_id, node)
                if node.attempt_id != attempt.attempt_id:
                    raise IntegrityError("end node belongs to another attempt")
            if record.status in ("completed", "paused"):
                nodes = self.list(Node, run_id=record.run_id)
                steps = self.list(Step, run_id=record.run_id)
                committed = {n.step_id for n in nodes}
                zero_step = False
                if record.last_node_id is None:
                    source = self.replay_source(record.run_id)
                    zero_step = (source is not None and source.start == source.end
                                 and not nodes and not steps)
                if (record.last_node_id is None and not zero_step) or any(
                    s.attempt_id == attempt.attempt_id and s.step_id not in committed for s in steps
                ):
                    raise IncompleteRecordError("clean termination requires a complete boundary")
        elif isinstance(record, StateCapture):
            attempt = self.get(Attempt, record.attempt_id)
            self._same_run(record.run_id, attempt)
            self.get(State, record.state_id)
            if record.step_id:
                step = self.get(Step, record.step_id)
                self._same_run(record.run_id, step)
                if step.attempt_id != record.attempt_id or step.index != record.index:
                    raise IntegrityError("state capture belongs to another step boundary")
            elif record.index != 0:
                raise IntegrityError("noninitial capture requires a step")
        elif isinstance(record, State):
            for dependency in (
                *record.dependencies,
                *((record.logical_parent_id,) if record.logical_parent_id else ()),
            ):
                self.get(State, dependency)
        elif isinstance(record, Failure):
            self._same_run(record.run_id, self.get(Attempt, record.attempt_id))
            if record.step_id:
                step = self.get(Step, record.step_id)
                self._same_run(record.run_id, step)
                if step.attempt_id != record.attempt_id:
                    raise IntegrityError("failure belongs to another attempt")
            if record.execution_id:
                execution = self.get(Execution, record.execution_id)
                operation = self.get(Operation, execution.operation_id)
                step = self.get(Step, operation.step_id)
                self._same_run(record.run_id, execution, operation, step)
                if step.attempt_id != record.attempt_id or (
                    record.step_id and record.step_id != step.step_id
                ):
                    raise IntegrityError("failure execution belongs to another boundary")

    @timed("store.save")
    def save(self, record: T) -> T:
        """Persist an immutable fact. Nodes and events have dedicated publication APIs."""
        if type(record) not in MODELS.values() or isinstance(record, (Event, Node)):
            raise TypeError("use append_event or commit_node for events/nodes")
        record = type(record).model_validate(record)
        with self._transaction():
            existing = self._existing(record)
            self._verify_refs([record])
            self._relations(record)
            if existing:
                return existing
            if isinstance(record, Step):
                self._attempt_open(record.attempt_id)
                tip = self._tip(record.run_id)
                if (tip.node_id if tip else None) != record.parent_node_id:
                    raise ConflictError("step parent is not the current node")
                if any(
                    s.attempt_id == record.attempt_id and s.index == record.index
                    for s in self.list(Step, run_id=record.run_id)
                ):
                    raise ConflictError("attempt already has this step index")
            elif isinstance(record, Operation):
                self._step_open(record.step_id)
                if record.tool_call_id and any(
                    op.step_id == record.step_id and op.tool_call_id == record.tool_call_id
                    for op in self.list(Operation, run_id=record.run_id)
                ):
                    raise ConflictError("duplicate provider tool-call identity in step")
            elif isinstance(record, Execution):
                operation = self.get(Operation, record.operation_id)
                self._step_open(operation.step_id)
                previous = [
                    e
                    for e in self.list(Execution, run_id=record.run_id)
                    if e.operation_id == record.operation_id
                ]
                if record.index != len(previous) + 1:
                    raise ConflictError("execution index must follow recorded attempts")
            elif isinstance(record, CallEvidence):
                execution = self.get(Execution, record.execution_id)
                operation = self.get(Operation, execution.operation_id)
                self._step_open(operation.step_id)
                history = self._execution_events(execution)
                if len(history) != 1:
                    raise ConflictError("internal evidence requires a dispatched, unfinished call")
                previous = [
                    e for e in self.list(CallEvidence, run_id=record.run_id)
                    if e.execution_id == record.execution_id
                ]
                if record.index != len(previous) + 1:
                    raise ConflictError("internal evidence index must follow recorded facts")
            elif isinstance(record, State):
                self._state_records(record, require_complete=False)
            elif isinstance(record, ToolGroup):
                self._step_open(record.step_id)
                if any(g.step_id == record.step_id for g in self.list(ToolGroup)):
                    raise ConflictError("step already has a declared tool group")
                operations = [
                    o for o in self.list(Operation, run_id=record.run_id)
                    if o.step_id == record.step_id
                ]
                if len(operations) != 1 or operations[0].kind != "model":
                    raise ConflictError("declare the group after one model operation, before tools")
            elif isinstance(record, RunEnd):
                self._attempt_open(record.attempt_id)
                tip = self._tip(record.run_id)
                if record.last_node_id != (tip.node_id if tip else None):
                    raise ConflictError("attempt end does not identify the current node")
            elif isinstance(record, StateCapture):
                self._attempt_open(record.attempt_id)
                if record.step_id:
                    self._step_open(record.step_id)
                if any(n.node_id == record.node_id for n in self.list(Node)) or any(
                    c.node_id == record.node_id for c in self.list(StateCapture)
                ):
                    raise ConflictError("boundary capture already exists or node is committed")
            self._insert(record)
        return record

    def _execution_events(
        self, execution: Execution, *, events: tuple[Event, ...] | None = None
    ) -> tuple[Event, ...]:
        events = self.list(Event, run_id=execution.run_id) if events is None else events
        events = sorted(
            (
                e
                for e in events
                if e.execution_id == execution.execution_id
            ),
            key=lambda e: e.sequence,
        )
        expected = ("dispatched", "result", "delivered", "accepted")
        if tuple(e.kind for e in events) != expected[: len(events)] or len(events) > len(expected):
            raise IntegrityError(f"invalid execution event order: {execution.execution_id}")
        return tuple(events)

    def execution_events(self, execution_id: str) -> tuple[Event, ...]:
        """Read a call's ordered facts and verify its request/result/observation bytes."""
        with self._artifacts.strict_checks(), self._snapshot():
            execution = self.get(Execution, execution_id)
            history = self._execution_events(execution)
            self._verify_refs([execution, *history])
            return history

    @timed("store.append_event")
    def append_event(
        self,
        execution_id: str,
        kind: EventKind,
        *,
        payload: ArtifactRef | None = None,
        details: ArtifactRef | None = None,
        outcome: Outcome | None = None,
    ) -> Event:
        with self._transaction():
            execution = self.get(Execution, execution_id)
            operation = self.get(Operation, execution.operation_id)
            self._step_open(operation.step_id)
            events = self.list(Event, run_id=execution.run_id)
            history = self._execution_events(execution, events=events)
            expected = ("dispatched", "result", "delivered", "accepted")
            if len(history) == len(expected) or kind != expected[len(history)]:
                raise IncompleteRecordError(
                    f"event {kind} cannot follow {[e.kind for e in history]}"
                )
            if kind == "accepted":
                other_ids = {
                    e.execution_id
                    for e in self.list(Execution, run_id=execution.run_id)
                    if e.operation_id == operation.operation_id and e.execution_id != execution_id
                }
                if any(
                    e.kind == "accepted" and e.execution_id in other_ids
                    for e in events
                ):
                    raise ConflictError("logical operation already has an accepted execution")
            sequence = (
                max((e.sequence for e in events), default=0) + 1
            )
            event = Event(
                run_id=execution.run_id,
                execution_id=execution_id,
                sequence=sequence,
                kind=kind,
                payload=payload,
                details=details,
                outcome=outcome,
            )
            # Recheck earlier payloads: a removed/corrupt result cannot be delivered.
            self._verify_refs([execution, *history, event])
            self._insert(event)
            return event

    def _state_records(
        self,
        state: State,
        *,
        require_complete: bool,
        active: set | None = None,
        seen: set | None = None,
    ) -> list[State]:
        active = active if active is not None else set()
        seen = seen if seen is not None else set()
        if state.state_id in active:
            raise IntegrityError("cyclic physical state dependencies")
        if state.state_id in seen:
            return []
        if require_complete and not state.capture.complete:
            raise IncompleteRecordError(f"incomplete capture: {state.state_id}")
        if state.backend == "btrfs-image.v1":
            from palinreplay.storage.btrfs.verification import verify_state

            verify_state(self, state)
        active.add(state.state_id)
        records = [state]
        for dependency in state.dependencies:
            records.extend(
                self._state_records(
                    self.get(State, dependency),
                    require_complete=require_complete,
                    active=active,
                    seen=seen,
                )
            )
        active.remove(state.state_id)
        seen.add(state.state_id)
        return records

    def _step_records(self, step: Step, calls: Node | StepCalls) -> list[Fact]:
        records: list[Fact] = []
        operations = [
            o for o in self.list(Operation, run_id=step.run_id) if o.step_id == step.step_id
        ]
        tool_ids = {o.operation_id for o in operations if o.kind == "tool"}
        if tool_ids != set(calls.tool_operation_ids):
            raise IncompleteRecordError("declared tool group differs from recorded operations")
        op_ids = {o.operation_id for o in operations}
        executions = [
            e for e in self.list(Execution, run_id=step.run_id) if e.operation_id in op_ids
        ]
        if {e.operation_id for e in executions} != op_ids:
            raise IncompleteRecordError("operation has no recorded execution")
        events_by_execution = {}
        for execution in executions:
            history = self._execution_events(execution)
            if len(history) < 2 or history[1].outcome == "unknown":
                raise IncompleteRecordError(
                    f"execution result pending or unknown: {execution.execution_id}"
                )
            if any(e.sequence > calls.accepted_event_sequence for e in history):
                raise IntegrityError("execution facts extend beyond node input boundary")
            events_by_execution[execution.execution_id] = history
            records.extend(history)
        selected = [calls.model_execution_id, *calls.tool_execution_ids]
        for execution_id in selected:
            if len(events_by_execution.get(execution_id, ())) != 4:
                raise IncompleteRecordError(
                    f"node execution has not been accepted: {execution_id}"
                )
        model = self.get(Execution, calls.model_execution_id)
        if self.get(Operation, model.operation_id).kind != "model":
            raise IntegrityError("node model execution is not a model operation")
        selected_tools = [self.get(Execution, item) for item in calls.tool_execution_ids]
        if tuple(e.operation_id for e in selected_tools) != calls.tool_operation_ids:
            raise IntegrityError("node tool executions do not match declared tool order")
        accepted = {
            execution_id
            for execution_id, history in events_by_execution.items()
            if len(history) == 4
        }
        if accepted != set(selected):
            raise IncompleteRecordError("node omits accepted executions")
        if (
            max(events_by_execution[item][-1].sequence for item in selected)
            != calls.accepted_event_sequence
        ):
            raise IntegrityError("node input boundary is not its last accepted event")
        records.extend([*operations, *executions])
        execution_ids = {e.execution_id for e in executions}
        evidence = [
            e for e in self.list(CallEvidence, run_id=step.run_id)
            if e.execution_id in execution_ids
        ]
        for item in evidence:
            self._relations(item)
        records.extend(evidence)
        groups = [g for g in self.list(ToolGroup, run_id=step.run_id) if g.step_id == step.step_id]
        if groups:
            if len(groups) != 1:
                raise IntegrityError("multiple tool-group declarations for step")
            group = groups[0]
            self._relations(group)
            model_ops = [o for o in operations if o.kind == "model"]
            declared = tuple(self.get(Operation, oid) for oid in calls.tool_operation_ids)
            if (
                len(model_ops) != 1
                or group.model_execution_id != calls.model_execution_id
                or tuple(o.tool_call_id for o in declared) != group.tool_call_ids
                or any(o.caused_by_execution_id != group.model_execution_id for o in declared)
            ):
                raise IncompleteRecordError("calls differ from the declared complete tool group")
            records.append(group)
        return records

    @timed("step.check_complete")
    def accepted_step(self, step_id: str) -> StepCalls:
        """Verify the declared whole group and all attempts before state capture starts."""
        with self._artifacts.strict_checks(), self._snapshot():
            step = self.get(Step, step_id)
            groups = [
                g for g in self.list(ToolGroup, run_id=step.run_id) if g.step_id == step_id
            ]
            if len(groups) != 1:
                raise IncompleteRecordError("step needs an explicit whole tool group, including empty")
            group = groups[0]
            operations = {
                o.tool_call_id: o for o in self.list(Operation, run_id=step.run_id)
                if o.step_id == step_id and o.kind == "tool"
            }
            if set(operations) != set(group.tool_call_ids):
                raise IncompleteRecordError("declared tool group has missing or extra calls")
            tools = tuple(operations[tool_id] for tool_id in group.tool_call_ids)
            selected = []
            for operation in tools:
                accepted = [
                    e for e in self.list(Execution, run_id=step.run_id)
                    if e.operation_id == operation.operation_id
                    and len(self._execution_events(e)) == 4
                ]
                if len(accepted) != 1:
                    raise IncompleteRecordError("each declared tool requires one accepted result")
                selected.append(accepted[0].execution_id)
            calls = StepCalls(
                model_execution_id=group.model_execution_id,
                tool_operation_ids=tuple(o.operation_id for o in tools),
                tool_execution_ids=tuple(selected),
                accepted_event_sequence=max(
                    (e.sequence for e in self.list(Event, run_id=step.run_id)), default=0
                ),
            )
            self._verify_refs(self._step_records(step, calls))
            return calls

    def _node_records(self, node: Node, seen: set | None = None) -> list[Fact]:
        run, attempt = self.get(Run, node.run_id), self.get(Attempt, node.attempt_id)
        self._same_run(node.run_id, attempt)
        records: list[Fact] = [run, attempt, node]
        captures = [c for c in self.list(StateCapture) if c.node_id == node.node_id]
        for capture in captures:
            if any(getattr(capture, key) != getattr(node, key) for key in (
                "run_id", "attempt_id", "step_id", "index", "state_id",
            )):
                raise IntegrityError("capture evidence and node boundaries differ")
        if len(captures) > 1:
            raise IntegrityError("multiple captures for one boundary")
        records.extend(captures)
        if node.index == 0 and self.recording_source(node.run_id) is not None:
            raise IntegrityError("source-reference recording cannot contain a local C0")
        if node.index:
            step = self.get(Step, node.step_id)
            self._relations(step)
            if (
                step.run_id != node.run_id
                or step.attempt_id != node.attempt_id
                or step.index != node.index
                or step.parent_node_id != node.parent_node_id
            ):
                raise IntegrityError("node and step boundaries differ")
            records.append(step)
            records.extend(self._step_records(step, node))
        records.extend(self._state_records(self.get(State, node.state_id), require_complete=True))
        self._verify_refs(records, seen)
        return records

    @timed("node.commit")
    def commit_node(self, node: Node) -> Node:
        """Validate all required material, then publish one immutable boundary atomically."""
        node = Node.model_validate(node)
        with self._external_checks(), self._transaction():
            existing = self._existing(node)
            self._node_records(node)
            if existing:
                return existing
            self._attempt_open(node.attempt_id)
            tip = self._tip(node.run_id)
            if node.index == 0:
                if tip is not None or self.list(Event, run_id=node.run_id):
                    raise ConflictError("C0 must precede execution history")
            elif (tip.node_id if tip else None) != node.parent_node_id or (
                tip is not None and node.index != tip.index + 1
            ):
                raise ConflictError("node does not extend the current history")
            cursor = max((e.sequence for e in self.list(Event, run_id=node.run_id)), default=0)
            if node.accepted_event_sequence != cursor:
                raise IncompleteRecordError(
                    "node input boundary does not match persisted event position"
                )
            if node.step_id and any(
                n.step_id == node.step_id for n in self.list(Node, run_id=node.run_id)
            ):
                raise ConflictError("step already has a committed node")
            self._insert(node)
        return node

    def get_node(self, node_id: str) -> Node:
        """Read a published node and verify its current material/dependency integrity."""
        with self._external_checks(), self._artifacts.strict_checks(), self._snapshot():
            node = self.get(Node, node_id)
            self._node_records(node)
            return node

    @timed("store.check_integrity")
    def check_integrity(self) -> IntegrityReport:
        """Audit persisted records and required bytes; unfinished tails are reported separately."""
        with self._external_checks(), self._artifacts.strict_checks(), self._snapshot():
            issues, records, seen = [], [], set()
            try:
                checks = [row[0] for row in self._db.execute("PRAGMA integrity_check")]
                if checks != ["ok"]:
                    issues.append(IntegrityIssue(record="catalog", message="; ".join(checks)))
                rows = self._db.execute("SELECT * FROM records ORDER BY rowid").fetchall()
            except sqlite3.DatabaseError as exc:
                return IntegrityReport(
                    issues=(IntegrityIssue(record="catalog", message=str(exc)),),
                    incomplete_steps=(),
                    pending_executions=(),
                    staged_artifacts=(),
                )
            for row in rows:
                try:
                    record = self._decode(row)
                    records.append(record)
                    self._relations(record)
                    self._verify_refs([record], seen)
                    if isinstance(record, Node):
                        self._node_records(record, seen)
                    elif isinstance(record, Execution):
                        self._execution_events(record)
                    elif isinstance(record, Run):
                        self.replay_finalization(record.run_id)
                    elif isinstance(record, State):
                        self._verify_refs(self._state_records(record, require_complete=False), seen)
                except (StoreError, OSError, sqlite3.DatabaseError) as exc:
                    issues.append(
                        IntegrityIssue(record=f"{row['kind']}/{row['id']}", message=str(exc))
                    )
            committed = {r.step_id for r in records if isinstance(r, Node)}
            for run_id in {r.run_id for r in records if isinstance(r, Event)}:
                sequences = sorted(
                    r.sequence for r in records if isinstance(r, Event) and r.run_id == run_id
                )
                if sequences != list(range(1, len(sequences) + 1)):
                    issues.append(
                        IntegrityIssue(record=f"run/{run_id}", message="event sequence has gaps")
                    )
            completed = {
                r.execution_id
                for r in records
                if isinstance(r, Event) and r.kind == "result" and r.outcome != "unknown"
            }
            staged = tuple(sorted(p.name for p in (self.root / "artifacts" / ".tmp").iterdir()))
            return IntegrityReport(
                issues=tuple(issues),
                incomplete_steps=tuple(
                    r.step_id for r in records if isinstance(r, Step) and r.step_id not in committed
                ),
                pending_executions=tuple(
                    r.execution_id
                    for r in records
                    if isinstance(r, Execution) and r.execution_id not in completed
                ),
                staged_artifacts=staged,
            )
