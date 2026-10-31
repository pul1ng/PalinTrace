import hashlib
import struct
import zlib

import pytest

import palinreplay.storage.codec as _json_codec
from palinreplay.core.serialization import json_bytes
from palinreplay.storage.store import IntegrityError, RecordStore


def test_deterministic_encoding_small_metadata_and_nonfinite_rejection(tmp_path):
    data = {"text": "repeated" * 1000, "path": "中文\udcff"}
    with RecordStore.create(tmp_path / "store") as store:
        first = store.put_json(data, compress=True)
        assert first == store.put_json(data, compress=True)
        assert store.read_json(first) == data
        assert store.put_json({"small": True}, compress=True).media_type == "application/json"
        with pytest.raises(ValueError):
            store.put_json({"nan": float("nan")}, compress=True)


@pytest.mark.parametrize("fault", [
    "magic", "truncated", "trailing", "concatenated", "short-length", "long-length",
    "limit", "checksum", "json", "unknown-version", "unknown-codec",
])
def test_invalid_compressed_metadata_is_rejected_even_with_a_valid_outer_digest(tmp_path, fault):
    raw = json_bytes({"text": "repeat" * 1000})
    data, media = _json_codec.compress(raw)
    if fault == "magic":
        data = b"PRJ2" + data[4:]
    elif fault == "truncated":
        data = data[:-1]
    elif fault == "trailing":
        data += b"trailing"
    elif fault == "concatenated":
        data += zlib.compress(raw)
    elif fault in {"short-length", "long-length", "limit"}:
        length = len(raw) + (-1 if fault == "short-length" else 1)
        if fault == "limit":
            length = _json_codec.MAX_JSON_BYTES + 1
        data = data[:4] + struct.pack(">Q", length) + data[12:]
    elif fault == "checksum":
        data = data[:-1] + bytes([data[-1] ^ 1])
    elif fault == "json":
        data = b"PRJ1" + struct.pack(">Q", 1) + zlib.compress(b"!")
    elif fault == "unknown-version":
        media = media.replace("version=1", "version=2")
    elif fault == "unknown-codec":
        media = media.replace("zlib", "other")
    with RecordStore.create(tmp_path / "store") as store:
        ref = store.put_bytes(data, media_type=media)
        store.verify_artifact(ref)
        assert hashlib.sha256(data).hexdigest() == ref.sha256
        with pytest.raises(IntegrityError):
            store.read_json(ref)
