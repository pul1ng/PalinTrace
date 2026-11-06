import base64
import json
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

import pytest

from palinreplay.core.errors import IntegrityError
from palinreplay.core.interfaces import Payload
from palinreplay.core.models import Node, Run
from palinreplay.core.replay_contract import source_reference
from palinreplay.core.resume_contract import resume_source_reference
from palinreplay.core.source_payload import source_payload
from palinreplay.record.selection import SourceSelection
from palinreplay.replay.config import replay_config
from palinreplay.resume.recording import branch_config
from palinreplay.storage.codec import JSON_MEDIA, ZLIB_MEDIA, compress
from palinreplay.storage.payloads import ROOT_MEDIA
from palinreplay.storage.store import RecordStore
from tests.helpers.recorder_smoke import FakeStateBackend


@pytest.fixture
def materials(tmp_path):
    with RecordStore.create(tmp_path / "source") as store:
        values = {
            role: {"role": role, "entries": [{"content": "repeated source message " * 20}] * 40}
            for role in ("config", "context", "input_position")
        }
        refs = {role: store.put_payload_json(value) for role, value in values.items()}
        assert all(ref.media_type == ROOT_MEDIA for ref in refs.values())
        run = Run(task_id="fixture", agent_name="fixture", agent_version="1", config=refs["config"])
        state = FakeStateBackend().capture(store, None, None)
        node = Node(run_id=run.run_id, attempt_id="source-attempt", index=0,
                    context=refs["context"], input_position=refs["input_position"],
                    state_id=state.state_id, accepted_event_sequence=0)
        physical = {role: store.read_bytes(ref) for role, ref in refs.items()}
        logical = {f"{role}_payload": store.read_payload(ref) for role, ref in refs.items()}
        return SimpleNamespace(
            **values, **logical, run=run, node=node, state=state,
            plan=SourceSelection("fixture-source", run, (node,), (state,)), steps=(),
            physical_payloads=physical,
            context_bytes=physical["context"], input_position_bytes=physical["input_position"],
        )


def contract(materials, mode):
    if mode == "replay":
        config = replay_config(materials)
        return config, config["replay"]["source_start"], source_reference
    session = SimpleNamespace(
        materials=materials,
        start=SimpleNamespace(source_store="fixture-source", model="fixture", max_steps=1),
    )
    config = branch_config(session)
    return config, config["resume"], resume_source_reference


@pytest.mark.parametrize("mode", ["replay", "resume"])
def test_shared_source_contract_pins_physical_root_and_exact_logical_bytes(materials, mode):
    config, start, validate = contract(materials, mode)
    source = validate(config)
    assert source.run == materials.run and source.start == materials.node
    assert source.states == (materials.state,)
    refs = {"config": materials.run.config, "context": materials.node.context,
            "input_position": materials.node.input_position}
    roles = ("context", "input_position") if mode == "replay" else tuple(refs)
    for role in roles:
        raw = base64.b64decode(start["raw_materials"][role], validate=True)
        logical = start["logical_materials"][role]
        assert raw == materials.physical_payloads[role]
        assert len(raw) == refs[role].size
        assert sha256(raw).hexdigest() == refs[role].sha256
        assert base64.b64decode(logical["data"], validate=True) == getattr(
            materials, f"{role}_payload").data
        assert logical["media_type"] == JSON_MEDIA
        assert source_payload(refs[role], raw, logical) == getattr(materials, role)


@pytest.mark.parametrize("mode", ["replay", "resume"])
@pytest.mark.parametrize("change", ["physical", "logical-hash", "logical-size", "media", "missing"])
def test_shared_source_contract_rejects_changed_materials(materials, mode, change):
    config, start, validate = contract(materials, mode)
    logical = start["logical_materials"]["context"]
    original = materials.context_payload.data
    if change == "physical":
        # Logical bytes must never be accepted under the physical root's digest.
        start["raw_materials"]["context"] = base64.b64encode(original).decode("ascii")
    elif change == "logical-hash":
        changed = original.replace(b"repeated", b"modified")
        assert len(changed) == len(original)
        logical["data"] = base64.b64encode(changed).decode("ascii")
    elif change == "logical-size":
        # Even JSON-equivalent alternate bytes do not have the pinned identity.
        logical["data"] = base64.b64encode(original + b" ").decode("ascii")
    elif change == "media":
        logical["media_type"] = "text/plain"
    else:
        del start["logical_materials"]["context"]
    with pytest.raises(IntegrityError):
        validate(config)


def test_replay_decoded_prefix_must_match_protected_logical_material(materials):
    config, start, validate = contract(materials, "replay")
    start["context"] = deepcopy(start["context"])
    start["context"]["entries"].append({"content": "unrecorded future message"})
    with pytest.raises(IntegrityError, match="decoded material"):
        validate(config)


@pytest.mark.parametrize("encoding", ["indented", "compressed"])
def test_legacy_source_material_retains_original_byte_identity(tmp_path, encoding):
    value = {"messages": [{"content": "original source bytes " * 100}] * 3}
    data = json.dumps(value, indent=2).encode("utf-8")
    media = JSON_MEDIA
    if encoding == "compressed":
        data, media = compress(data)
        assert media == ZLIB_MEDIA
    with RecordStore.create(tmp_path / "source") as store:
        ref = store.put_payload(Payload(data, media))
        assert ref.media_type == media
        assert source_payload(ref, store.read_bytes(ref)) == value
        with pytest.raises(ValueError, match="reference"):
            source_payload(ref, data + b" ")
