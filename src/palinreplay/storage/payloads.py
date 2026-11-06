"""Lossless JSON payloads backed by immutable, content-addressed subtrees.

Only the registered root media type denotes an envelope. User JSON is always
inside an explicit value token; dictionaries resembling references have no
special meaning. Array blocks have a fixed fanout so appending changes only the
last block and its ancestors, while every repeated position is retained.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import ValidationError

from palinreplay.core.errors import IntegrityError
from palinreplay.core.interfaces import Payload
from palinreplay.core.models import ArtifactRef
from palinreplay.core.serialization import json_bytes
from palinreplay.storage.artifacts import ArtifactStore, sync_directory

ROOT_MEDIA = "application/vnd.palinreplay.payload+json;version=1"
NODE_MEDIA = "application/vnd.palinreplay.payload-node+json;version=1"
FORMAT = "palinreplay.payload.v1"
FANOUT = 32
INLINE_BYTES = 8192
_CACHE_BYTES = 8 * 1024 * 1024
_RESERVED = ("application/vnd.palinreplay.payload+", "application/vnd.palinreplay.payload-node+")


def is_shared(media_type: str) -> bool:
    """Recognize root envelopes, rejecting unknown versions of reserved media."""
    if media_type == ROOT_MEDIA:
        return True
    if media_type.startswith(_RESERVED):
        raise IntegrityError("unsupported shared payload media type or version")
    return False


def _json(data: bytes):
    try:
        return json.loads(data)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise IntegrityError("invalid shared payload JSON") from error


def _ref(value) -> ArtifactRef:
    try:
        ref = ArtifactRef.model_validate(value)
    except (ValidationError, TypeError) as error:
        raise IntegrityError("invalid shared payload reference") from error
    if ref.media_type != NODE_MEDIA:
        raise IntegrityError("shared payload child has an unsupported media type")
    return ref


def _token(value, *, recursive=True):
    if not isinstance(value, list) or not value or not isinstance(value[0], str):
        raise IntegrityError("invalid shared payload value token")
    kind = value[0]
    if kind == "value" and len(value) == 2:
        if recursive:
            try:
                json_bytes(value[1])
            except (TypeError, ValueError, RecursionError) as error:
                raise IntegrityError("invalid shared payload inline value") from error
    elif kind == "ref" and len(value) == 2:
        _ref(value[1])
    elif kind == "object" and len(value) == 2 and isinstance(value[1], list):
        previous = None
        for pair in value[1]:
            if (not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str)
                    or (previous is not None and pair[0] <= previous)):
                raise IntegrityError("invalid shared payload object entries")
            previous = pair[0]
            if recursive:
                _token(pair[1])
    elif kind == "list" and len(value) == 2 and isinstance(value[1], list):
        if len(value[1]) > FANOUT:
            raise IntegrityError("shared payload inline sequence exceeds its fanout")
        if recursive:
            for child in value[1]:
                _token(child)
    elif kind == "array" and len(value) == 4:
        count, height = value[1:3]
        if (type(count) is not int or count < 1 or type(height) is not int
                or height < 0 or height > 64 or count > FANOUT ** (height + 1)):
            raise IntegrityError("invalid shared payload array shape")
        _ref(value[3])
    else:
        raise IntegrityError("unknown or malformed shared payload value token")


def _root(data: bytes):
    root = _json(data)
    if (not isinstance(root, dict) or set(root) != {"format", "logical", "value"}
            or root["format"] != FORMAT):
        raise IntegrityError("invalid shared payload root")
    try:
        logical = ArtifactRef.model_validate(root["logical"])
        _token(root["value"])
    except (ValidationError, TypeError, RecursionError) as error:
        raise IntegrityError("invalid shared payload root descriptor") from error
    if logical.media_type != "application/json":
        raise IntegrityError("unsupported shared payload logical media type")
    return logical, root["value"]


def logical_identity(data: bytes, media_type: str) -> tuple[str, int, str]:
    """Describe original bytes from a checked root descriptor or raw material.

    This does not prove that referenced subtrees exist. Readers holding a store
    must additionally expand/verify the payload before trusting its contents.
    """
    if is_shared(media_type):
        logical, _ = _root(data)
        return logical.sha256, logical.size, logical.media_type
    return hashlib.sha256(data).hexdigest(), len(data), media_type


class PayloadStore:
    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts
        self._scope = ContextVar("shared_payload_scope", default=None)

    @contextmanager
    def scope(self):
        """Retain bounded parsed data only during one read/recording boundary.

        Every reuse still verifies the physical file through ArtifactStore. Its
        existing boundary-local proofs may avoid rehashing unchanged files.
        This cache never itself grants permission to trust persisted bytes.
        """
        if self._scope.get() is not None:
            yield
            return
        token = self._scope.set(_Cache())
        try:
            yield
        finally:
            self._scope.reset(token)

    def put(self, payload: Payload) -> ArtifactRef:
        if not isinstance(payload, Payload):
            raise TypeError("captured payload must be a Payload")
        # Restrict optimization to exact canonical round trips. Whitespace,
        # duplicate keys, alternate numeric spellings and opaque media survive.
        if payload.media_type != "application/json" or len(payload.data) <= INLINE_BYTES:
            return self.artifacts.put(payload.data, payload.media_type)
        try:
            value = json.loads(payload.data)
            canonical = json_bytes(value)
        except (ValueError, UnicodeError, TypeError, RecursionError):
            return self.artifacts.put(payload.data, payload.media_type)
        if canonical != payload.data:
            return self.artifacts.put(payload.data, payload.media_type)
        try:
            with self.scope():
                token = self._encode(value, encoded=canonical)
        except (ValueError, RecursionError):
            # Deep but otherwise valid canonical JSON remains a raw payload.
            return self.artifacts.put(payload.data, payload.media_type)
        root = {
            "format": FORMAT,
            "logical": {
                "sha256": hashlib.sha256(payload.data).hexdigest(),
                "size": len(payload.data),
                "media_type": payload.media_type,
            },
            "value": token,
        }
        return self.artifacts.put(json_bytes(root), ROOT_MEDIA)

    def _save(self, value, *, encoded=None):
        data = json_bytes(value) if encoded is None else encoded
        cache = self._scope.get()
        ref = cache.published.get(data) if cache is not None else None
        if ref is not None:
            self.artifacts.verify_internal(ref)
            sync_directory(self.artifacts.root / ref.sha256[:2])
        else:
            ref = self.artifacts.put(data, NODE_MEDIA)
            if cache is not None:
                cache.remember(cache.published, data, ref, len(data))
        return ref.model_dump(mode="json")

    def _encode(self, value, depth=0, *, encoded=None):
        data = json_bytes(value) if encoded is None else encoded
        if len(data) <= INLINE_BYTES:
            return ["value", value]
        if depth > 64:
            raise ValueError("payload exceeds the shared encoding depth")
        if isinstance(value, list):
            if len(value) <= FANOUT:
                items = [self._encode(item, depth + 1) for item in value]
                token = ["list", items]
                if len(json_bytes(token)) <= INLINE_BYTES:
                    return token
                return ["array", len(value), 0, self._save(["items", items])]
            level = [self._save(["items", [self._encode(item, depth + 1)
                                         for item in value[start:start + FANOUT]]])
                     for start in range(0, len(value), FANOUT)]
            height = 0
            while len(level) > 1:
                level = [self._save(["branch", level[start:start + FANOUT]])
                         for start in range(0, len(level), FANOUT)]
                height += 1
            return ["array", len(value), height, level[0]]
        token = (["object", [[key, self._encode(item, depth + 1)]
                             for key, item in sorted(value.items())]]
                 if isinstance(value, dict) else ["value", value])
        encoded = json_bytes(token)
        return (["ref", self._save(token, encoded=encoded)]
                if len(encoded) > INLINE_BYTES else token)

    def read(self, ref: ArtifactRef, *, data: bytes | None = None) -> Payload:
        if not is_shared(ref.media_type):
            return Payload(self.artifacts.read(ref) if data is None else data, ref.media_type)
        return _Reader(self.artifacts, ref, data=data, cache=self._scope.get()).read()

    def iter_artifacts(self, ref: ArtifactRef) -> Iterator[ArtifactRef]:
        if not is_shared(ref.media_type):
            yield ref
            return
        reader = _Reader(self.artifacts, ref, cache=self._scope.get())
        reader.read()
        yield from reader.refs.values()


class _Cache:
    def __init__(self):
        self.published = OrderedDict()
        self.nodes = OrderedDict()
        self.sizes = {}
        self.size = 0

    def remember(self, target, key, value, size):
        if size > _CACHE_BYTES or key in target:
            return
        target[key] = value
        identity = (id(target), key)
        self.sizes[identity] = size
        self.size += size
        # Both entry count and bytes are bounded, including very small nodes.
        while self.size > _CACHE_BYTES or len(target) > 1024:
            oldest, _ = target.popitem(last=False)
            self.size -= self.sizes.pop((id(target), oldest))


class _Reader:
    """Expand one root; reconstructed values remain private to this operation."""

    def __init__(self, artifacts, root, *, data=None, cache=None):
        self.artifacts = artifacts
        self.root = root
        self.root_data = data
        self.cache = cache
        self.refs = {}
        self.nodes = {}
        self.values = {}
        self.key_sizes = {}
        self.maximum = None

    def _load(self, ref):
        key = (ref.sha256, ref.size, ref.media_type)
        if key not in self.nodes:
            if self.cache is not None and key in self.cache.nodes:
                self.artifacts.verify_internal(ref)
                self.nodes[key] = self.cache.nodes[key]
            else:
                self.nodes[key] = _json(self.artifacts.read(ref))
                if self.cache is not None:
                    self.cache.remember(self.cache.nodes, key, self.nodes[key], ref.size)
            self.refs[key] = ref
        return self.nodes[key]

    def _bounded(self, size):
        if size > self.maximum:
            raise IntegrityError("shared payload expansion exceeds its logical size")
        return size

    def _decode(self, token, depth=0):
        if depth > 256:
            raise IntegrityError("shared payload nesting exceeds the supported depth")
        _token(token, recursive=False)
        kind = token[0]
        if kind == "value":
            value = token[1]
            return value, self._bounded(len(json_bytes(value)) - 1)
        if kind == "ref":
            ref = _ref(token[1])
            key = (ref.sha256, ref.size, ref.media_type)
            if key not in self.values:
                self.values[key] = self._decode(self._load(ref), depth + 1)
            return self.values[key]
        if kind == "object":
            result, size = {}, 2
            for key, child in token[1]:
                value, length = self._decode(child, depth + 1)
                if key not in self.key_sizes:
                    self.key_sizes[key] = len(json_bytes(key))
                size += self.key_sizes[key] + length + bool(result)
                self._bounded(size)
                result[key] = value
            return result, size
        if kind == "list":
            result, size = [], 2
            for child in token[1]:
                value, length = self._decode(child, depth + 1)
                size += length + bool(result)
                self._bounded(size)
                result.append(value)
            return result, size
        count, height, ref = token[1:]
        result, size = self._array(_ref(ref), height, depth + 1)
        if len(result) != count:
            raise IntegrityError("shared payload array count mismatch")
        return result, size

    def _array(self, ref, height, depth):
        if depth > 256:
            raise IntegrityError("shared payload nesting exceeds the supported depth")
        node = self._load(ref)
        kind = "items" if height == 0 else "branch"
        if (not isinstance(node, list) or len(node) != 2 or node[0] != kind
                or not isinstance(node[1], list) or not 1 <= len(node[1]) <= FANOUT):
            raise IntegrityError("invalid shared payload sequence block")
        result, size = [], 2
        for child in node[1]:
            if height:
                values, length = self._array(_ref(child), height - 1, depth + 1)
                size += length - 2 + bool(result)
                self._bounded(size)
                result.extend(values)
            else:
                value, length = self._decode(child, depth + 1)
                size += length + bool(result)
                self._bounded(size)
                result.append(value)
        return result, size

    def read(self):
        # Root bytes are checked by ArtifactStore before schema interpretation.
        data = self.artifacts.read(self.root) if self.root_data is None else self.root_data
        self.refs[(self.root.sha256, self.root.size, self.root.media_type)] = self.root
        logical, token = _root(data)
        self.maximum = logical.size
        try:
            value, _ = self._decode(token)
            data = json_bytes(value)
        except (ValueError, TypeError, RecursionError) as error:
            raise IntegrityError("invalid shared payload expansion") from error
        if len(data) != logical.size or hashlib.sha256(data).hexdigest() != logical.sha256:
            raise IntegrityError("shared payload logical size/hash mismatch")
        return Payload(data, logical.media_type)
