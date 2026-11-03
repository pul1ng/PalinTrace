"""Per-attempt Replay receipts, independent of the closed SQLite connection.

A receipt confirms preceding cleanup/publication operations. Its checksum detects
corruption; it is neither a signature nor proof of its own last I/O acknowledgement.
"""

import json
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from palinreplay.core.errors import IntegrityError
from palinreplay.core.models import ArtifactRef, Attempt, Identity, RunEnd
from palinreplay.core.replay_outcome import execution_status
from palinreplay.core.serialization import json_bytes
from palinreplay.storage.io import publish_receipt_json, read_receipt_bytes

PROTOCOL = "store-receipt-v1"


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["palinreplay.replay-finalization.v1"]
    run_id: Identity
    config: ArtifactRef
    attempt: Attempt
    run_end: RunEnd | None
    execution_status: Literal["completed", "stopped", "cancelled", "failed"]
    faults: list[dict]
    cancellations: list[dict]
    failure_ids: list[Identity]
    resource_closes: dict[str, bool]
    publications: dict[str, bool]


def receipt_path(root, attempt):
    # Attempt is a validated model; identities cannot contain path separators.
    return root / ("replay-finalization-" + attempt.attempt_id + ".json")


def envelope(payload):
    data = Receipt.model_validate(payload).model_dump(mode="json")
    return {"payload": data, "sha256": sha256(json_bytes(data)).hexdigest()}


def publish(root, payload):
    receipt = Receipt.model_validate(payload)
    return publish_receipt_json(receipt_path(root, receipt.attempt), envelope(payload))


def read_receipt(root, run, attempt, end, failures):
    path = receipt_path(root, attempt)
    try:
        raw = read_receipt_bytes(path, missing_ok=True)
    except IntegrityError as error:
        raise IntegrityError("unreadable Replay finalization receipt") from error
    if raw is None:
        return None
    try:
        document = json.loads(raw)
        if set(document) != {"payload", "sha256"}:
            raise ValueError("invalid envelope")
        payload = document["payload"]
        if sha256(json_bytes(payload)).hexdigest() != document["sha256"]:
            raise ValueError("checksum differs")
        receipt = Receipt.model_validate(payload)
        if (receipt.run_id != run.run_id or receipt.config != run.config
                or receipt.attempt != attempt or receipt.run_end != end
                or receipt.failure_ids != [f.failure_id for f in failures]):
            raise ValueError("receipt differs from Run/config/Attempt/RunEnd/Failure facts")
        status = receipt.execution_status
        if status != execution_status(status, faults=receipt.faults, cancellations=receipt.cancellations):
            raise ValueError("receipt suppresses outcome evidence")
        if status != "failed" and (
            not {"recorder", "restore", "store"} <= receipt.resource_closes.keys()
            or not all(receipt.resource_closes.values())
            or not {"report.json", "r1-validation.json"} <= receipt.publications.keys()
            or not all(receipt.publications.values())
        ):
            raise ValueError("receipt suppresses a finalization fault")
        if status != "failed" and end is None:
            raise ValueError("confirmed execution requires a RunEnd")
        if status == "completed" and end.status != "completed":
            raise ValueError("completed receipt requires a completed Record boundary")
        return receipt.model_dump(mode="json")
    except (ValueError, TypeError, KeyError, ValidationError) as error:
        raise IntegrityError(f"invalid Replay finalization receipt: {error}") from error
