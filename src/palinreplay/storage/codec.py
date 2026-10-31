"""Versioned, bounded JSON envelope; content packs and logical schemas are unchanged."""

import struct
import zlib

from palinreplay.core.errors import IntegrityError
from palinreplay.diagnostics.metrics import phase

JSON_MEDIA = "application/json"
ZLIB_MEDIA = "application/vnd.palinreplay.json+zlib;version=1"
JSON_TYPES = {JSON_MEDIA, ZLIB_MEDIA}
MAGIC = b"PRJ1"
MAX_JSON_BYTES = 512 * 1024 * 1024


def compress(data):
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("JSON exceeds the compressed metadata size limit")
    if len(data) < 4096:
        return data, JSON_MEDIA
    with phase("metadata.compress"):
        encoded = MAGIC + struct.pack(">Q", len(data)) + zlib.compress(data, level=1)
    return (encoded, ZLIB_MEDIA) if len(encoded) < len(data) else (data, JSON_MEDIA)


def decode(data, media_type):
    if media_type != ZLIB_MEDIA:
        if media_type.startswith("application/vnd.palinreplay.json+"):
            raise IntegrityError("unsupported compressed JSON version or codec")
        return data
    with phase("metadata.decompress"):
        if len(data) < 12 or data[:4] != MAGIC:
            raise IntegrityError("invalid compressed JSON envelope")
        expected = struct.unpack(">Q", data[4:12])[0]
        if expected > MAX_JSON_BYTES:
            raise IntegrityError("compressed JSON exceeds the decoded size limit")
        try:
            decoder = zlib.decompressobj()
            result = decoder.decompress(data[12:], expected + 1)
        except zlib.error as error:
            raise IntegrityError("corrupt compressed JSON stream") from error
        if (len(result) != expected or not decoder.eof
            or decoder.unused_data or decoder.unconsumed_tail):
            raise IntegrityError("truncated, oversized or trailing compressed JSON data")
        return result
