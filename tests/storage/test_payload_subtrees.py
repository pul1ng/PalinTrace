import hashlib
import json
import weakref
from collections import Counter

import pytest

import palinreplay.storage.payloads as payloads
from palinreplay.core.interfaces import Payload
from palinreplay.core.serialization import json_bytes
from palinreplay.storage.payloads import NODE_MEDIA, ROOT_MEDIA
from palinreplay.storage.store import IntegrityError, RecordStore
from tests.storage.test_payload_expansion_cache import damage
from tests.storage.test_payloads import path


def values(kind):
    shared = ([{"content": "shared text " * 900, "index": i} for i in range(40)]
              if kind == "array" else
              {f"field_{i:03d}": {"content": "x" * 1000, "index": i} for i in range(100)})
    return shared, [{"shared": shared, "marker": i} for i in (1, 2)]


def child(store, ref, kind):
    return next(item for item in store.iter_payload_artifacts(ref)
                if item.media_type == NODE_MEDIA
                and json.loads(store.read_bytes(item))[0] == kind)


@pytest.mark.parametrize("kind", ["object", "array"])
def test_encoding_reuses_shared_subtree_without_changing_refs(tmp_path, monkeypatch, kind):
    shared, inputs = values(kind)
    calls = []
    original = payloads.PayloadStore._encode_value

    def encode(writer, value, depth):
        if value == shared:
            calls.append(depth)
        return original(writer, value, depth)

    monkeypatch.setattr(payloads.PayloadStore, "_encode_value", encode)
    with RecordStore.create(tmp_path / "cached") as store:
        with store._expansion_scope():
            refs = [store.put_payload_json(value) for value in inputs]
        assert calls == [1]
        assert [store.read_json(ref) for ref in refs] == inputs
        assert store._payloads._scope.get() is None
        assert store._payloads._expanded.get() is None
    calls.clear()
    with RecordStore.create(tmp_path / "separate") as store:
        assert [store.put_payload_json(value) for value in inputs] == refs
        assert calls == [1, 1]


@pytest.mark.parametrize("kind", ["object", "array"])
def test_different_roots_reuse_decoded_subtrees_and_keep_private_values(
    tmp_path, monkeypatch, kind,
):
    _, inputs = values(kind)
    counts = Counter()
    original_decode, original_array = payloads._Reader._decode, payloads._Reader._array_value

    def decode(reader, token, depth=0):
        if token[0] == "object" and token[1] and token[1][0][0].startswith("field_"):
            counts[(json_bytes(token), depth)] += 1
        return original_decode(reader, token, depth)

    def array(reader, ref, height, depth):
        counts[(ref.sha256, height, depth)] += 1
        return original_array(reader, ref, height, depth)

    with RecordStore.create(tmp_path / "store") as store:
        refs = [store.put_payload_json(value) for value in inputs]
        closures = [tuple(store.iter_payload_artifacts(ref)) for ref in refs]
        monkeypatch.setattr(payloads._Reader, "_decode", decode)
        monkeypatch.setattr(payloads._Reader, "_array_value", array)
        with store.snapshot():
            first = store.read_json(refs[0])
            first["shared"].clear()
            assert store.read_json(refs[1]) == inputs[1]
            assert counts and max(counts.values()) == 1
            assert store._payloads._scope.get().decoded
            assert store.read_json(refs[0]) == inputs[0]
            assert [tuple(store.iter_payload_artifacts(ref)) for ref in refs] == closures
        counts.clear()
        with store.snapshot():
            assert store.read_json(refs[1]) == inputs[1]
            assert counts  # A new operation must reconstruct its own entries.


@pytest.mark.parametrize("node_kind", ["items", "value"])
@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_encoded_subtree_reuse_rechecks_transitive_material(tmp_path, node_kind, fault):
    _, inputs = values("array")
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_payload_json(inputs[0])
        target = child(store, ref, node_kind)
        with pytest.raises(IntegrityError), store._expansion_scope():
            assert store.put_payload_json(inputs[0]) == ref
            damage(store, target, fault)
            store.put_payload_json(inputs[1])
        assert store._payloads._scope.get() is None
        assert store._payloads._expanded.get() is None


@pytest.mark.parametrize("scope", ["snapshot", "_verification_scope"])
@pytest.mark.parametrize("fault", ["missing", "corrupt"])
@pytest.mark.parametrize("node_kind", ["items", "value"])
def test_decoded_subtree_reuse_rechecks_transitive_material(
    tmp_path, scope, fault, node_kind,
):
    _, inputs = values("array")
    with RecordStore.create(tmp_path / "store") as store:
        refs = [store.put_payload_json(value) for value in inputs]
        target = child(store, refs[0], node_kind)
        with getattr(store, scope)():
            assert store.read_json(refs[0]) == inputs[0]
            damage(store, target, fault)
            with pytest.raises(IntegrityError):
                store.read_payload(refs[1])


@pytest.mark.parametrize("field", ["size", "sha256"])
def test_failed_root_never_publishes_decoded_entries(tmp_path, field):
    _, inputs = values("array")
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_payload_json(inputs[0])
        root = json.loads(store.read_bytes(ref))
        root["logical"][field] = 1 if field == "size" else "0" * 64
        bad = store.put_bytes(json_bytes(root), media_type=ROOT_MEDIA)
        with store.snapshot():
            with pytest.raises(IntegrityError):
                store.read_payload(bad)
            assert not store._payloads._scope.get().decoded
            assert store.read_json(ref) == inputs[0]
            assert store._payloads._scope.get().decoded


def test_warm_subtree_keeps_root_size_and_decode_depth_limits(tmp_path):
    value = "shared text " * 900
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_payload_json(value)
        root = json.loads(store.read_bytes(ref))
        small = json.loads(store.read_bytes(ref))
        small["logical"]["size"] = 1
        small_ref = store.put_bytes(json_bytes(small), media_type=ROOT_MEDIA)
        for _ in range(256):
            root["value"] = ["list", [root["value"]]]
            value = [value]
        logical = json_bytes(value)
        root["logical"].update(size=len(logical), sha256=hashlib.sha256(logical).hexdigest())
        deep_ref = store.put_bytes(json_bytes(root), media_type=ROOT_MEDIA)
        with store.snapshot():
            assert store.read_payload(ref).data != logical
            before = dict(store._payloads._scope.get().decoded)
            with pytest.raises(IntegrityError, match="logical size"):
                store.read_payload(small_ref)
            with pytest.raises(IntegrityError, match="nesting"):
                store.read_payload(deep_ref)
            assert dict(store._payloads._scope.get().decoded) == before


def test_subtree_memos_are_bounded_and_released_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(payloads, "_CACHE_BYTES", 64 * 1024)
    _, inputs = values("array")
    with RecordStore.create(tmp_path / "store") as store:
        with pytest.raises(RuntimeError, match="fixture end"), store._expansion_scope():
            cache = store._payloads._scope.get()
            tracked = weakref.ref(cache)
            for value in inputs:
                payload = Payload.json(value)
                ref = store.put_payload(payload)
                assert store.read_payload(ref) == payload
                assert cache.size <= payloads._CACHE_BYTES
                assert not cache.encoding_refs
            del cache
            raise RuntimeError("fixture end")
        assert tracked() is None
        assert store._payloads._scope.get() is None
        assert store._payloads._expanded.get() is None


def test_warm_array_does_not_bypass_its_declared_height(tmp_path):
    _, inputs = values("array")
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_payload_json(inputs[0])
        root = json.loads(store.read_bytes(ref))
        shared = dict(root["value"][1])["shared"]
        assert shared[0] == "array"
        shared[2] += 1
        bad = store.put_bytes(json_bytes(root), media_type=ROOT_MEDIA)
        with store.snapshot():
            assert store.read_json(ref) == inputs[0]
            with pytest.raises(IntegrityError, match="sequence block"):
                store.read_json(bad)


def test_shallow_local_reference_cannot_validate_a_deep_cached_array(tmp_path):
    with RecordStore.create(tmp_path / "store") as store:
        text = "shared text " * 900
        original = store.put_payload_json(text)
        token = json.loads(store.read_bytes(original))["value"]
        block = store.put_bytes(json_bytes(["items", [token]]), media_type=NODE_MEDIA)
        nested = ["array", 1, 0, block.model_dump(mode="json")]
        value = [text]
        for _ in range(253):
            nested = ["list", [nested]]
            value = [value]
        refs = []
        for content, fields in [({"a": text, "b": value}, [["a", token], ["b", nested]]),
                                ({"b": value}, [["b", nested]])]:
            data = json_bytes(content)
            root = {"format": payloads.FORMAT,
                    "logical": {"sha256": hashlib.sha256(data).hexdigest(),
                                "size": len(data), "media_type": "application/json"},
                    "value": ["object", fields]}
            refs.append(store.put_bytes(json_bytes(root), media_type=ROOT_MEDIA))
        with store.snapshot():
            assert store.read_json(original) == text
            before = dict(store._payloads._scope.get().decoded)
            for ref in refs:
                with pytest.raises(IntegrityError, match="nesting"):
                    store.read_payload(ref)
                assert dict(store._payloads._scope.get().decoded) == before


def test_encoding_memo_does_not_bypass_depth_raw_fallback(tmp_path):
    with RecordStore.create(tmp_path / "store") as store, store._expansion_scope():
        value = {"first": {"leaf": "shared text " * 900}}
        store.put_payload_json(value)
        for _ in range(63):
            value = {"nested": value}
        payload = Payload.json(value)
        ref = store.put_payload(payload)
        assert ref.media_type == payload.media_type
        assert path(store, ref).read_bytes() == payload.data
