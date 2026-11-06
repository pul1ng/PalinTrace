import hashlib
import json
import sqlite3
import tempfile

import pytest

import palinreplay.storage.payloads as payload_storage
from palinreplay.core.interfaces import Payload
from palinreplay.core.models import Run
from palinreplay.core.serialization import json_bytes
from palinreplay.storage.payloads import FANOUT, NODE_MEDIA, ROOT_MEDIA, logical_identity
from palinreplay.storage.store import IntegrityError, RecordStore


def messages(count=70):
    return [{"role": "user", "content": f"message {i}: " + "long\n中\udcff" * 200}
            for i in range(count)]


def path(store, ref):
    return store.root / "artifacts" / ref.sha256[:2] / ref.sha256[2:]


def closure(store, ref):
    return {item.sha256: item for item in store.iter_payload_artifacts(ref)}


@pytest.mark.parametrize("payload", [
    Payload(b"\x00\xff\r\n", "application/octet-stream"),
    Payload(b'{ "x":1,"x":2 }\r\n', "application/json"),
    Payload(b'{"x":1.000,"y":1e2}\n', "application/json"),
    Payload(b'{"x":-0}\n', "application/json"),
    Payload(b'{"a":NaN}\n', "application/json"),
    Payload(b'not valid json', "application/json"),
    Payload(b'{"\xff":1}', "application/json"),
    Payload.json({"small": [True, None, 1.0, -0.0]}),
    Payload(json_bytes(messages()), "application/json;charset=utf-8"),
])
def test_raw_fallback_preserves_exact_bytes_and_original_media(tmp_path, payload):
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_payload(payload)
        assert ref.sha256 == hashlib.sha256(payload.data).hexdigest()
        assert ref.size == len(payload.data)
        assert ref.media_type == payload.media_type
        assert store.read_bytes(ref) == payload.data
        assert store.read_payload(ref) == payload
        assert list(store.iter_payload_artifacts(ref)) == [ref]
        assert logical_identity(payload.data, payload.media_type) == (
            ref.sha256, ref.size, ref.media_type,
        )


def test_shared_physical_and_logical_identity_are_distinct_and_exact(tmp_path):
    payload = Payload.json({"messages": messages(), "temperature": 0.0})
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_payload(payload)
        physical = store.read_bytes(ref)
        assert ref.media_type == ROOT_MEDIA
        assert ref.sha256 == hashlib.sha256(physical).hexdigest()
        assert ref.size == len(physical)
        assert physical != payload.data
        assert logical_identity(physical, ref.media_type) == (
            hashlib.sha256(payload.data).hexdigest(), len(payload.data), payload.media_type,
        )
        assert store.read_payload(ref) == payload
        assert store.read_json(ref) == json.loads(payload.data)
        assert store.put_payload(payload) == ref
        # Raw metadata APIs keep their existing physical contract in v2.
        raw = store.put_json(json.loads(payload.data))
        assert raw.media_type == "application/json"
        assert store.read_bytes(raw) == payload.data


@pytest.mark.parametrize("count", [FANOUT ** 2, FANOUT ** 2 + FANOUT])
def test_append_shares_prefix_blocks_and_keeps_bounded_branch_fanout(tmp_path, count):
    before = [f"item {index:016d}" for index in range(count)]
    with RecordStore.create(tmp_path / "store") as store:
        first = store.put_payload_json(before)
        old = closure(store, first)
        second = store.put_payload_json([*before, 9999])
        new = closure(store, second)
        assert store.read_json(first) == before
        assert store.read_json(second) == [*before, 9999]
        # Prefix blocks survive both ordinary appends and a tree height change.
        assert len(old.keys() & new.keys()) >= FANOUT + 1
        assert len(new.keys() - old.keys()) <= 4
        for ref in new.values():
            if ref.media_type == NODE_MEDIA:
                node = json.loads(store.read_bytes(ref))
                if node[0] in {"items", "branch"}:
                    assert 1 <= len(node[1]) <= FANOUT


def test_arbitrary_edits_and_duplicate_positions_preserve_old_snapshots(tmp_path):
    original = messages(80)
    original[1]["content"] += "stable large message" * 1000
    original[35] = original[4]
    edited = [*original]
    edited[2] = {"role": "system", "content": "new instructions" * 200}
    edited[6:10] = [original[4], original[4]]
    edited.reverse()
    with RecordStore.create(tmp_path / "store") as store:
        old_ref = store.put_payload_json(original)
        new_ref = store.put_payload_json(edited)
        assert store.read_payload(old_ref) == Payload.json(original)
        assert store.read_payload(new_ref) == Payload.json(edited)
        assert closure(store, old_ref).keys() & closure(store, new_ref).keys()
        # Future writes never replace or extend a previous payload's context.
        later_ref = store.put_payload_json(edited + messages(5))
        assert store.read_json(old_ref) == original
        assert store.read_json(new_ref) == edited
        assert store.read_json(later_ref) == edited + messages(5)


def test_reference_shaped_user_objects_are_ordinary_data(tmp_path):
    value = {
        "$ref": {"sha256": "f" * 64, "size": 12000, "media_type": NODE_MEDIA},
        "messages": [
            {"$ref": "missing", "value": ["ref", {"content": "large" * 1000}]},
            ["array", 100, 2, {"sha256": "0" * 64}],
        ],
    }
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_payload_json(value)
        assert store.read_json(ref) == value
        store.verify_artifact(ref)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_each_read_and_integrity_check_reverifies_the_entire_closure(tmp_path, damage):
    with RecordStore.create(tmp_path / "store") as store:
        payload = Payload.json({"messages": messages()})
        ref = store.put_payload(payload)
        run = store.save(Run(task_id="fixture", agent_name="fake", agent_version="1", config=ref))
        refs = closure(store, ref)
        leaf = next(item for item in refs.values()
                    if item.media_type == NODE_MEDIA and item.size > 2000)
        assert store.read_payload(ref) == payload
        assert store.check_integrity().ok
        if damage == "missing":
            path(store, leaf).unlink()
        else:
            data = path(store, leaf).read_bytes()
            path(store, leaf).chmod(0o600)
            path(store, leaf).write_bytes(data[:-2] + b"!\n")
            path(store, leaf).chmod(0o400)
        # The root's physical digest alone is still valid after child damage.
        assert hashlib.sha256(store.read_bytes(ref)).hexdigest() == ref.sha256
        for action in (
            lambda: store.read_payload(ref), lambda: store.read_json(ref),
            lambda: store.verify_artifact(ref), lambda: store._verify_refs([run]),
            lambda: list(store.iter_payload_artifacts(ref)),
        ):
            with pytest.raises(IntegrityError):
                action()
        assert not store.check_integrity().ok


@pytest.mark.parametrize("field", ["sha256", "size"])
def test_expansion_checks_logical_identity_even_with_valid_root_digest(tmp_path, field):
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_payload_json(messages())
        root = json.loads(store.read_bytes(ref))
        root["logical"][field] = "0" * 64 if field == "sha256" else root["logical"][field] + 1
        forged = store.put_bytes(json_bytes(root), media_type=ROOT_MEDIA)
        with pytest.raises(IntegrityError, match="logical size/hash"):
            store.read_payload(forged)
        with pytest.raises(IntegrityError):
            store.verify_artifact(forged)


def test_closure_export_is_self_contained_and_distinct(tmp_path):
    value = [messages(1)[0]] * 100
    with RecordStore.create(tmp_path / "origin") as source:
        ref = source.put_payload_json(value)
        refs = list(source.iter_payload_artifacts(ref))
        assert refs[0] == ref
        assert len(refs) == len(set(refs))
        assert len(refs) < 10
        with RecordStore.create(tmp_path / "copy") as copied:
            for item in refs:
                assert copied.put_bytes(source.read_bytes(item), media_type=item.media_type) == item
            assert copied.read_payload(ref) == Payload.json(value)
            copied.verify_artifact(ref)


@pytest.mark.parametrize("layout, version", [
    ("shared", "palinreplay.store.v2"), ("flat", "palinreplay.store.v1"),
])
def test_store_layout_version_survives_reopen_and_future_writes(tmp_path, layout, version):
    location = tmp_path / "store"
    payload = Payload.json(messages())
    with RecordStore.create(location, payload_layout=layout) as store:
        ref = store.put_payload(payload)
        assert store.payload_layout == layout
        assert (ref.media_type == ROOT_MEDIA) == (layout == "shared")
    with sqlite3.connect(location / "catalog.sqlite") as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == (
            version,
        )
    with RecordStore.open(location, read_only=False) as reopened:
        assert reopened.payload_layout == layout
        assert reopened.read_payload(ref) == payload
        assert reopened.put_payload(payload) == ref
        new = reopened.put_payload_json(messages(71))
        assert (new.media_type == ROOT_MEDIA) == (layout == "shared")


def test_unknown_store_version_and_layout_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="payload_layout"):
        RecordStore.create(tmp_path / "invalid", payload_layout="unknown")
    assert not (tmp_path / "invalid").exists()
    with RecordStore.create(tmp_path / "store"):
        pass
    with sqlite3.connect(tmp_path / "store" / "catalog.sqlite") as db:
        db.execute("UPDATE metadata SET value='palinreplay.store.v999'")
    with pytest.raises(IntegrityError, match="schema version"):
        RecordStore.open(tmp_path / "store")


@pytest.mark.parametrize("data, media", [
    (b"{}\n", ROOT_MEDIA),
    (b"{}\n", ROOT_MEDIA.replace("version=1", "version=2")),
])
def test_malformed_or_future_shared_descriptor_fails_closed(tmp_path, data, media):
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_bytes(data, media_type=media)
        with pytest.raises(IntegrityError):
            store.read_payload(ref)
        with pytest.raises(IntegrityError):
            store.verify_artifact(ref)
        with pytest.raises(IntegrityError):
            logical_identity(data, media)


@pytest.mark.parametrize("payload", [Payload(b'{', "application/json"), Payload(b"opaque")])
def test_malformed_ordinary_run_configuration_remains_compatible(tmp_path, payload):
    with RecordStore.create(tmp_path / "store") as store:
        run = store.save(Run(
            task_id="fixture", agent_name="fake", agent_version="1", config=store.put_payload(payload),
        ))
        assert store.recording_source(run.run_id) is None
        assert store.check_integrity().ok


def test_deep_canonical_json_uses_lossless_raw_fallback(tmp_path):
    value = "large" * 3000
    for _ in range(80):
        value = {"nested": value}
    payload = Payload.json(value)
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_payload(payload)
        assert ref.media_type == "application/json"
        assert store.read_payload(ref) == payload


def test_snapshot_reuses_parsing_but_rechecks_files_and_expires_its_cache(tmp_path, monkeypatch):
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_payload_json(messages())
        parsed = []
        original = payload_storage._json

        def observe(data):
            parsed.append(len(data))
            return original(data)

        monkeypatch.setattr(payload_storage, "_json", observe)
        with store.snapshot():
            value = store.read_json(ref)
            first = len(parsed)
            assert first > 1
            value[0]["content"] = "mutating a returned value must not change cached nodes"
            assert store.read_json(ref) == messages()
            assert len(parsed) == first + 1  # Only the independently checked root reparses.
        assert store._payloads._scope.get() is None
        assert store.read_json(ref) == messages()
        assert len(parsed) == first * 2 + 1


@pytest.mark.parametrize("scope", ["snapshot", "_verification_scope"])
def test_cached_nodes_do_not_hide_corruption_within_a_boundary(tmp_path, scope):
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_payload_json(messages())
        refs = list(store.iter_payload_artifacts(ref))
        child = next(item for item in refs if item.media_type == NODE_MEDIA)
        with getattr(store, scope)():
            assert store.read_json(ref) == messages()
            assert store.read_json(ref) == messages()
            data = path(store, child).read_bytes()
            path(store, child).chmod(0o600)
            path(store, child).write_bytes(data[:-2] + b"!\n")
            path(store, child).chmod(0o400)
            with pytest.raises(IntegrityError):
                store.read_payload(ref)


def test_scope_cache_is_bounded_without_changing_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(payload_storage, "_CACHE_BYTES", 16 * 1024)
    with RecordStore.create(tmp_path / "store") as store, store._verification_scope():
        for count in range(60, 65):
            payload = Payload.json(messages(count))
            ref = store.put_payload(payload)
            assert store.read_payload(ref) == payload
            assert store._payloads._scope.get().size <= 16 * 1024


def test_nested_snapshot_shares_cache_and_error_discards_it(tmp_path):
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_payload_json(messages())
        with pytest.raises(RuntimeError, match="boundary failed"), store.snapshot():
            cache = store._payloads._scope.get()
            assert store.read_json(ref) == messages()
            with store.snapshot():
                assert store._payloads._scope.get() is cache
                assert store.read_json(ref) == messages()
            raise RuntimeError("boundary failed")
        assert store._payloads._scope.get() is None
        assert not store._db.in_transaction
        with store.snapshot():
            assert store._payloads._scope.get() is not cache
            assert store.read_json(ref) == messages()


def test_read_only_snapshot_cache_creates_no_artifacts_or_clock_files(tmp_path, monkeypatch):
    location = tmp_path / "store"
    with RecordStore.create(location) as store:
        ref = store.put_payload_json(messages())
    before = {path.relative_to(location): path.stat().st_mtime_ns
              for path in (location / "artifacts").rglob("*")}

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only payload cache must not create a clock file")

    monkeypatch.setattr(tempfile, "TemporaryFile", forbidden)
    with RecordStore.open(location) as store, store.snapshot():
        assert store.read_json(ref) == messages()
        assert store.read_json(ref) == messages()
    after = {path.relative_to(location): path.stat().st_mtime_ns
             for path in (location / "artifacts").rglob("*")}
    assert after == before


def test_parsed_cache_obeys_strict_physical_checks(tmp_path, monkeypatch):
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_payload_json(messages())
        verified = []
        original = store._artifacts.verify

        def observe(item):
            verified.append(item)
            return original(item)

        with store._verification_scope():
            assert store.read_json(ref) == messages()
            assert store.read_json(ref) == messages()
            monkeypatch.setattr(store._artifacts, "verify", observe)
            with store._artifacts.strict_checks():
                assert store.read_json(ref) == messages()
        assert verified and all(item.media_type == NODE_MEDIA for item in verified)
