"""Validate an inline logical prefix against its pinned physical artifact."""

import base64
import json
from hashlib import sha256

from palinreplay.storage.codec import JSON_TYPES, decode
from palinreplay.storage.payloads import ROOT_MEDIA, logical_identity


def source_payload(ref, physical, logical=None):
    if len(physical) != ref.size or sha256(physical).hexdigest() != ref.sha256:
        raise ValueError("source payload differs from its protected material reference")
    if ref.media_type == ROOT_MEDIA:
        expected_hash, expected_size, media = logical_identity(physical, ref.media_type)
        if not isinstance(logical, dict) or logical.get("media_type") != media:
            raise ValueError("shared source payload requires its exact logical representation")
        data = base64.b64decode(logical["data"], validate=True)
        if len(data) != expected_size or sha256(data).hexdigest() != expected_hash:
            raise ValueError("source payload differs from its logical identity")
    else:
        data, media = physical, ref.media_type
    if media not in JSON_TYPES:
        raise ValueError("source prefix requires a supported JSON media type")
    return json.loads(decode(data, media))
