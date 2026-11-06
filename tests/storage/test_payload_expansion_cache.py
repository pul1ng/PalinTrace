import os
import weakref

import pytest

import palinreplay.storage.payloads as payload_storage
from palinreplay.core.interfaces import Payload
from palinreplay.core.models import Run
from palinreplay.storage.payloads import NODE_MEDIA
from palinreplay.storage.store import IntegrityError, RecordStore


def fixture(store):
    payload = Payload.json({"messages": [{"content": "message " * 2000}] * 40})
    ref = store.put_payload(payload)
    refs = tuple(store.iter_payload_artifacts(ref))
    child = next(item for item in refs if item.media_type == NODE_MEDIA)
    return payload, ref, refs, child


def count_expansions(monkeypatch):
    calls = []
    original = payload_storage._Reader.read

    def read(reader):
        calls.append(reader.root)
        return original(reader)

    monkeypatch.setattr(payload_storage._Reader, "read", read)
    return calls


def materialize(store, ref, method):
    if method == "closure":
        return tuple(store.iter_payload_artifacts(ref))
    return getattr(store, method)(ref)


def damage(store, ref, kind):
    path = store.root / "artifacts" / ref.sha256[:2] / ref.sha256[2:]
    if kind == "missing":
        path.unlink()
    else:
        stamp = path.stat()
        data = path.read_bytes()
        path.chmod(0o600)
        path.write_bytes(data[:-2] + b"!\n")
        path.chmod(0o400)
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))


def test_node_boundary_expands_once_across_all_material_readers(tmp_path, monkeypatch):
    with RecordStore.create(tmp_path / "store") as store:
        payload, ref, refs, _ = fixture(store)
        run = store.save(Run(task_id="fixture", agent_name="fixture", agent_version="1", config=ref))
        calls = count_expansions(monkeypatch)
        with store._verification_scope():
            assert store.read_payload(ref) == payload
            store._verify_refs([run])
            store.verify_artifact(ref)
            assert materialize(store, ref, "closure") == refs
            assert store.read_payload(ref) == payload
            assert calls == [ref]
        assert store._payloads._scope.get() is None
        assert store._payloads._expanded.get() is None
        with store._verification_scope():
            assert store.read_payload(ref) == payload
            assert calls == [ref, ref]


@pytest.mark.parametrize("scope", ["snapshot", "_verification_scope"])
@pytest.mark.parametrize("method", ["read_payload", "verify_artifact", "closure"])
@pytest.mark.parametrize("kind", ["missing", "corrupt"])
def test_cached_expansion_checks_every_child(tmp_path, scope, method, kind):
    with RecordStore.create(tmp_path / "store") as store:
        payload, ref, _, child = fixture(store)
        with getattr(store, scope)():
            assert store.read_payload(ref) == payload
            materialize(store, ref, method)
            damage(store, child, kind)
            with pytest.raises(IntegrityError):
                materialize(store, ref, method)


@pytest.mark.parametrize("method", ["read_payload", "verify_artifact", "closure"])
def test_cached_expansion_checks_the_root(tmp_path, method):
    with RecordStore.create(tmp_path / "store") as store:
        payload, ref, _, _ = fixture(store)
        with store._verification_scope():
            assert store.read_payload(ref) == payload
            damage(store, ref, "corrupt")
            with pytest.raises(IntegrityError):
                materialize(store, ref, method)


def test_strict_checks_reverify_full_closure_without_reconstructing_json(tmp_path, monkeypatch):
    with RecordStore.create(tmp_path / "store") as store:
        payload, ref, refs, _ = fixture(store)
        calls = count_expansions(monkeypatch)
        with store._verification_scope():
            assert store.read_payload(ref) == payload
            checked = []
            original = store._artifacts.verify

            def verify(item):
                checked.append(item)
                return original(item)

            monkeypatch.setattr(store._artifacts, "verify", verify)
            with store._artifacts.strict_checks():
                store.verify_artifact(ref)
            assert set(checked) == set(refs)
            assert calls == [ref]


def test_expansion_failure_is_not_cached_and_scope_error_releases_data(tmp_path, monkeypatch):
    with RecordStore.create(tmp_path / "store") as store:
        payload, ref, _, child = fixture(store)
        calls = count_expansions(monkeypatch)
        with pytest.raises(RuntimeError, match="boundary failed"), store._verification_scope():
            assert store.read_payload(ref) == payload
            active = store._payloads._expanded.get()
            assert active
            with store.snapshot():
                assert store.read_payload(ref) == payload
                assert store._payloads._expanded.get() is active
                assert calls == [ref]
            raise RuntimeError("boundary failed")
        assert store._payloads._scope.get() is None
        assert store._payloads._expanded.get() is None
        damage(store, child, "corrupt")
        with store._verification_scope():
            for _ in range(2):
                with pytest.raises(IntegrityError):
                    store.read_payload(ref)
                assert not store._payloads._expanded.get()
        assert calls == [ref, ref, ref]


def test_expanded_payload_is_released_at_boundary_end(tmp_path):
    with RecordStore.create(tmp_path / "store") as store:
        _, ref, _, _ = fixture(store)
        with store._verification_scope():
            value = store.read_payload(ref)
            tracked = weakref.ref(value)
            del value
            assert tracked() is not None
        assert tracked() is None
        assert store._payloads._expanded.get() is None


def test_read_only_snapshot_keeps_existing_parsed_cache_behavior(tmp_path, monkeypatch):
    with RecordStore.create(tmp_path / "store") as store:
        payload, ref, _, _ = fixture(store)
        calls = count_expansions(monkeypatch)
        with store.snapshot():
            assert store.read_payload(ref) == store.read_payload(ref) == payload
            assert store._payloads._expanded.get() is None
            assert calls == [ref, ref]


def test_current_boundary_working_set_is_not_evicted_by_parsed_cache_limit(tmp_path, monkeypatch):
    values = [Payload.json({"label": label, "messages": ["large content " * 1200] * 300})
              for label in ("first", "second")]
    assert sum(len(payload.data) for payload in values) > payload_storage._CACHE_BYTES
    with RecordStore.create(tmp_path / "store") as store:
        refs = [store.put_payload(payload) for payload in values]
        calls = count_expansions(monkeypatch)
        with store._verification_scope():
            for _ in range(2):
                for ref, expected in zip(refs, values, strict=True):
                    assert store.read_payload(ref) == expected
            assert calls == refs
            assert store._payloads._scope.get().size <= payload_storage._CACHE_BYTES
        assert store._payloads._expanded.get() is None
