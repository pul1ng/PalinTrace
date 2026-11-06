"""Selecting and validating saved R1 materials without creating a restore container."""

import hashlib
import sqlite3
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

import palinreplay.record.restore as restore
import palinreplay.sandbox.client as sandbox_client
import palinreplay.storage.btrfs.verification as verification
from palinreplay.core.errors import RecordUsageError
from palinreplay.core.interfaces import Payload
from palinreplay.core.models import Attempt, Node, Run, State
from palinreplay.core.serialization import json_bytes
from palinreplay.sandbox.runtime import C0LaunchConfig
from palinreplay.storage.btrfs.models import BACKEND, BtrfsRuntime, PoolLocation, Snapshot
from palinreplay.storage.codec import ZLIB_MEDIA
from palinreplay.storage.store import IntegrityError, RecordStore
from tests.storage.test_store import begin_step, call, candidate


def _record_run(store, name, *, payloads=None, oracle_sha256="e" * 64,
                agent_name="fixture", agent_version="1"):
    payloads = payloads or {}

    def put_payload(role, default):
        value = payloads.get(role)
        return store.put_bytes(value.data, media_type=value.media_type) if value else default()

    identity = uuid4()
    location = PoolLocation(
        volume="palinreplay-btrfs-" + identity.hex,
        uuid=identity,
        daemon_id="fixture-daemon",
        helper_image_id="sha256:" + "a" * 64,
    )
    launch = C0LaunchConfig(
        platform="linux/amd64",
        environment={"PATH": "/bin"},
        omitted_environment_keys=(),
        workdir="/",
        user="",
        shell=("bash", "-c"),
        entrypoint=("sleep",),
        command=("infinity",),
        network_mode="none",
        memory_bytes=0,
        nano_cpus=0,
    )
    runtime = BtrfsRuntime(
        source_container_id="b" * 64,
        source_image_id="sha256:" + "c" * 64,
        bootstrap_image_id="sha256:" + "d" * 64,
        launch=launch,
        c0_oracle_sha256=oracle_sha256,
    )
    run = store.save(Run(
        run_id=name,
        task_id="fixture",
        agent_name=agent_name,
        agent_version=agent_version,
        config=put_payload("config", lambda: store.put_json(
            {"name": name, "text": "repeated " * 700}, compress=True
        )),
    ))
    attempt = store.save(Attempt(run_id=run.run_id))
    extra = store.put_bytes(b"extra state material")

    def save_state(snapshot):
        manifest = store.put_json(snapshot.model_dump(mode="json"))
        return store.save(State(
            backend=BACKEND,
            capture=verification.capture_info(),
            manifest=manifest,
            runtime=store.put_json(runtime.model_dump(mode="json")),
            artifacts=(manifest, extra),
        ))

    c0_state = save_state(Snapshot(location=location, name="bases/C0", uuid=uuid4()))
    c0 = store.commit_node(Node(
        run_id=run.run_id,
        attempt_id=attempt.attempt_id,
        index=0,
        context=put_payload("context", lambda: store.put_json({"messages": ["initial"]})),
        input_position=put_payload("input_position", lambda: store.put_json({"messages": 1})),
        state_id=c0_state.state_id,
        accepted_event_sequence=0,
    ))
    c1_state = save_state(Snapshot(
        location=location, name="checkpoints/" + uuid4().hex, uuid=uuid4()
    ))
    step = begin_step(store, run, attempt, c0)
    model = call(store, step, kind="model")
    c1 = store.commit_node(candidate(store, step, c1_state, model))
    return run, c0, c1, c0_state, extra


@pytest.fixture
def record(tmp_path, monkeypatch):
    # Committing the fixture normally checks an external pool. Material loading must not.
    monkeypatch.setattr(verification, "verify_state", lambda store, state: None)
    path = tmp_path / "record"
    with RecordStore.create(path) as store:
        first = _record_run(store, "one")
        second = _record_run(store, "two")
    return path, first, second


@pytest.fixture
def no_container(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("material loading attempted to create or verify a container")

    monkeypatch.setattr(verification, "verify_state", unexpected)
    monkeypatch.setattr(sandbox_client, "connect_docker", unexpected)
    monkeypatch.setattr(restore.BtrfsPool, "__enter__", unexpected)
    monkeypatch.setattr(restore.BtrfsWorkspace, "start", unexpected)
    monkeypatch.setattr(restore.RestoredNode, "__init__", unexpected)
    return unexpected


def _rewrite_record(path, record):
    data = json_bytes(record.model_dump(mode="json"))
    kind = type(record).__name__.lower()
    key = getattr(record, f"{kind}_id")
    with sqlite3.connect(path / "catalog.sqlite") as catalog:
        catalog.execute(
            "UPDATE records SET body=?, sha256=? WHERE kind=? AND id=?",
            (data.decode("utf-8"), hashlib.sha256(data).hexdigest(), kind, key),
        )


def test_reads_selected_c0_step_and_full_id_from_one_snapshot(record, no_container, monkeypatch):
    path, (run, c0, c1, _, _), (_, other_c0, _, _, _) = record
    with RecordStore.open(path) as store:
        c0_materials = restore.load_restore_materials(store, "C0", run_id=run.run_id)
        c1_materials = restore.load_restore_materials(store, "C1", run_id=run.run_id)
        by_id = restore.load_restore_materials(store, c1.node_id)
        other = restore.load_restore_materials(store, other_c0.node_id)
        assert not store._closed

    assert c0_materials.run == run
    assert c0_materials.node == c0
    assert c0_materials.context == {"messages": ["initial"]}
    assert c0_materials.input_position == {"messages": 1}
    assert c0_materials.snapshot.name == "bases/C0"
    assert c0_materials.config["name"] == "one"
    assert c0_materials.run.config.media_type.endswith("version=1")
    assert c0_materials.runtime.launch.environment == {"PATH": "/bin"}
    assert c1_materials.node == c1
    assert c1_materials.context == {"messages": ["step", 1]}
    assert c1_materials.snapshot.name.startswith("checkpoints/")
    assert by_id == c1_materials
    assert other.node == other_c0
    monkeypatch.setattr(verification, "verify_state", lambda store, state: None)
    with RecordStore.open(path, read_only=False) as store:
        next_step = begin_step(store, run, store.get(Attempt, c1.attempt_id), c1)
        next_model = call(store, next_step, kind="model")
        c2 = store.commit_node(candidate(store, next_step, store.get(State, c1.state_id), next_model))
    assert c2.node_id != c1_materials.node.node_id
    assert c1_materials.context == {"messages": ["step", 1]}
    with pytest.raises(FrozenInstanceError):
        c0_materials.node = c1
    c0_materials.context["messages"].append("local change")
    with RecordStore.open(path) as store:
        assert restore.load_restore_materials(store, c0.node_id).context == {
            "messages": ["initial"]
        }
    assert not (path.parent / "restore.json").exists()


@pytest.mark.parametrize("selector,run_id,reason", [
    ("C99", "one", "no committed node"),
    ("C0", "unknown", "no committed node"),
    ("C0", None, "matches 2 committed nodes"),
    ("C1", None, "matches 2 committed nodes"),
])
def test_selection_fails_without_creating_resources(record, no_container, selector, run_id, reason):
    path = record[0]
    with RecordStore.open(path) as store:
        with pytest.raises(RecordUsageError, match=reason):
            restore.load_restore_materials(store, selector, run_id=run_id)
    assert not (path.parent / "restore.json").exists()


@pytest.mark.parametrize("selector,run_id", [
    ("full-id", None), ("C0", "one"), ("C0", None),
])
def test_node_selection_checks_only_matching_metadata(record, no_container, selector, run_id):
    path, (_, c0, c1, _, _), (_, other_c0, _, _, _) = record
    with sqlite3.connect(path / "catalog.sqlite") as catalog:
        catalog.execute("UPDATE records SET sha256=? WHERE kind='node' AND id=?", (
            "0" * 64, c1.node_id,
        ))
        # Even an unrelated run's C0 need not decode when the ID or run is explicit.
        catalog.execute("UPDATE records SET sha256=? WHERE kind='node' AND id=?", (
            "0" * 64, other_c0.node_id,
        ))
    with RecordStore.open(path) as store:
        if run_id is None and selector == "C0":
            with pytest.raises(RecordUsageError, match="matches 2 committed nodes"):
                restore.load_restore_materials(store, selector)
        else:
            value = c0.node_id if selector == "full-id" else selector
            assert restore.load_restore_materials(store, value, run_id=run_id).node == c0
        with pytest.raises(IntegrityError, match="metadata hash mismatch"):
            restore.load_restore_materials(store, c1.node_id)


def test_full_id_with_wrong_run_fails(record, no_container):
    path, (_, c0, _, _, _), _ = record
    with RecordStore.open(path) as store:
        with pytest.raises(RecordUsageError, match="no committed node"):
            restore.load_restore_materials(store, c0.node_id, run_id="two")


@pytest.mark.parametrize("role", [
    "run config", "node context", "node input position", "snapshot manifest",
    "Btrfs runtime", "state artifact",
])
@pytest.mark.parametrize("fault", ["missing", "wrong digest"])
def test_corrupt_required_material_fails(record, no_container, role, fault):
    path, (run, c0, _, state, extra), _ = record
    refs = {
        "run config": run.config,
        "node context": c0.context,
        "node input position": c0.input_position,
        "snapshot manifest": state.manifest,
        "Btrfs runtime": state.runtime,
        "state artifact": extra,
    }
    ref = refs[role]
    target = path / "artifacts" / ref.sha256[:2] / ref.sha256[2:]
    target.unlink()
    if fault == "wrong digest":
        target.write_bytes(b"x" * ref.size)
    with RecordStore.open(path) as store:
        with pytest.raises(IntegrityError, match=role):
            restore.load_restore_materials(store, c0.node_id)
    assert not (path.parent / "restore.json").exists()


@pytest.mark.parametrize("role,content,reason", [
    ("config", b"not json", "run config"),
    ("context", b"not json", "node context"),
    ("input_position", b"not json", "node input position"),
    ("manifest", b"not json", "snapshot manifest"),
    ("runtime", b"not json", "Btrfs runtime"),
    ("manifest", b"{}", "snapshot manifest"),
    ("runtime", b"{}", "Btrfs runtime"),
])
def test_valid_digest_but_invalid_json_or_schema_fails(
    record, no_container, role, content, reason
):
    path, (run, c0, _, state, _), _ = record
    with RecordStore.open(path, read_only=False) as store:
        ref = store.put_bytes(content, media_type="application/json")
    owner = run if role == "config" else c0 if role in ("context", "input_position") else state
    _rewrite_record(path, owner.model_copy(update={role: ref}))
    with RecordStore.open(path) as store:
        if role in ("config", "context", "input_position"):
            materials = restore.load_restore_materials(store, c0.node_id)
            assert getattr(materials, f"{role}_payload") == Payload(content, "application/json")
            with pytest.raises(IntegrityError, match="not valid JSON"):
                getattr(materials, role)
        else:
            with pytest.raises(IntegrityError, match=reason):
                restore.load_restore_materials(store, c0.node_id)


def test_invalid_compressed_config_with_valid_digest_fails_when_decoded(record, no_container):
    path, (run, c0, _, _, _), _ = record
    with RecordStore.open(path, read_only=False) as store:
        ref = store.put_bytes(b"invalid compressed material", media_type=ZLIB_MEDIA)
    _rewrite_record(path, run.model_copy(update={"config": ref}))
    with RecordStore.open(path) as store:
        materials = restore.load_restore_materials(store, c0.node_id)
        assert materials.config_payload == Payload(b"invalid compressed material", ZLIB_MEDIA)
        with pytest.raises(IntegrityError, match="compressed JSON"):
            _ = materials.config


@pytest.mark.parametrize("change", [
    {"backend": "fake-files-v1"},
    {"capture": verification.capture_info().model_copy(update={"scope": "other"})},
    {"dependencies": ("missing-dependency",)},
])
def test_unsupported_materials_fail(record, no_container, change):
    path, (_, c0, _, state, _), _ = record
    _rewrite_record(path, state.model_copy(update=change))
    with RecordStore.open(path) as store:
        with pytest.raises(IntegrityError, match="unsupported Btrfs"):
            restore.load_restore_materials(store, c0.node_id)


def test_missing_record_and_metadata_digest_fail(record, no_container):
    path, (run, c0, _, _, _), _ = record
    with sqlite3.connect(path / "catalog.sqlite") as catalog:
        catalog.execute("DELETE FROM records WHERE kind='run' AND id=?", (run.run_id,))
    with RecordStore.open(path) as store:
        with pytest.raises(IntegrityError, match="missing record: Run"):
            restore.load_restore_materials(store, c0.node_id)
    with sqlite3.connect(path / "catalog.sqlite") as catalog:
        catalog.execute("UPDATE records SET sha256=? WHERE kind='node' AND id=?", (
            "0" * 64, c0.node_id,
        ))
    with RecordStore.open(path) as store:
        with pytest.raises(IntegrityError, match="metadata hash mismatch"):
            restore.load_restore_materials(store, c0.node_id)
