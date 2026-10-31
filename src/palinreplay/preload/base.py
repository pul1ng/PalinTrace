"""Read and select an existing C0 base for an independent recording."""

import hashlib
import json
from pathlib import Path

from palinreplay.core.errors import IntegrityError, RecordUsageError
from palinreplay.core.serialization import json_bytes
from palinreplay.preload.input import load_preload_task
from palinreplay.sandbox.docker import _missing
from palinreplay.sandbox.runtime import _environment
from palinreplay.storage.btrfs.models import BACKEND, BtrfsC0, FrozenBtrfs
from palinreplay.storage.io import _read_metadata, write_json


def open_preload(path):
    root = Path(path).resolve()
    raw = _read_metadata(root / "c0.json")
    digest = hashlib.sha256(raw).hexdigest()
    if json.loads(_read_metadata(root / "c0.checksum.json")) != {"sha256": digest}:
        raise IntegrityError("Btrfs preload metadata checksum mismatch")
    try:
        metadata = BtrfsC0.model_validate_json(raw)
    except ValueError as error:
        raise IntegrityError("invalid Btrfs preload metadata") from error
    if metadata.snapshot.name != "bases/C0":
        raise IntegrityError("preload must select the immutable C0 snapshot")
    return FrozenBtrfs(root, metadata, digest)


class BtrfsPreloadRecord:
    def __init__(self, base, inputs):
        self.base, self.inputs = base, inputs

    @classmethod
    def select(cls, path, binding):
        base = open_preload(path)
        inputs = load_preload_task(binding.task.root.parent, binding.task.spec.task_id)
        if (
            base.metadata.input_sha256
            != hashlib.sha256(json_bytes(inputs.model_dump(mode="json"))).hexdigest()
        ):
            raise RecordUsageError("Btrfs preload input differs from the selected task")
        return cls(base, inputs)

    def environment(self, client):
        launch = self.base.metadata.launch
        values = launch.environment | self.inputs.env
        missing = set(launch.omitted_environment_keys) - values.keys()
        if missing:
            try:
                image = client.images.get(self.base.metadata.source_image_id)
            except Exception as error:
                if not _missing(error):
                    raise
                raise RecordUsageError(
                    "preload needs omitted environment values from the source image; restore that image"
                ) from error
            original = _environment(image.attrs.get("Config") or {})
            if missing - original.keys():
                raise RecordUsageError("preload environment values are unavailable")
            values.update({key: original[key] for key in missing})
        return values

    def describe(self):
        return {
            "preload_id": "preload:sha256:" + self.base.metadata_sha256,
            "backend": BACKEND,
            "path": str(self.base.path),
            "pool": self.base.metadata.snapshot.location.model_dump(mode="json"),
            "material_verification": "identity-and-on-read-checksums",
        }

    def attach(self, store):
        write_json(
            store.root / "btrfs.json", self.base.metadata.snapshot.location.model_dump(mode="json")
        )
