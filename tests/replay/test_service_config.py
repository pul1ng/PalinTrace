import json

import pytest
from pydantic import ValidationError

from palinreplay.restoration.service_config import load_services


def service(name="A", **overrides):
    return {"name": name, "start_command": "start " + name,
            "readiness_command": "probe " + name, "timeout_sec": 10, **overrides}


def load(tmp_path, values):
    path = tmp_path / "services.json"
    path.write_text(json.dumps({"services": values}))
    return load_services(path)


def test_single_service_preserves_commands_and_inherits_execution_settings(tmp_path):
    config = load(tmp_path, [service(start_command="  launch --flag  ")])
    item, = config.services
    assert item.name == "A" and item.start_command == "  launch --flag  "
    assert item.cwd is None and item.env == {} and item.timeout_sec == 10
    assert sorted(path.name for path in tmp_path.iterdir()) == ["services.json"]


def test_two_services_preserve_order_and_explicit_overrides(tmp_path):
    config = load(tmp_path, [service("B", cwd="/srv/B", env={"FLAG": "value"}), service("A")])
    assert [item.name for item in config.services] == ["B", "A"]
    assert config.services[0].cwd == "/srv/B" and config.services[0].env == {"FLAG": "value"}
    assert config.services[1].env == {}


@pytest.mark.parametrize("field", ["name", "start_command", "timeout_sec"])
def test_missing_required_field_fails_without_creating_runtime_resources(tmp_path, field):
    item = service()
    del item[field]
    with pytest.raises(ValidationError, match=field):
        load(tmp_path, [item])
    assert sorted(path.name for path in tmp_path.iterdir()) == ["services.json"]


@pytest.mark.parametrize("overrides", [
    {"name": " "}, {"start_command": ""}, {"readiness_command": "\x00"},
    {"timeout_sec": 0}, {"timeout_sec": -1}, {"timeout_sec": float("inf")},
    {"timeout_sec": True}, {"timeout_sec": "10"}, {"cwd": "relative"},
    {"env": {"FLAG": 1}}, {"env": {"A=B": "value"}}, {"extra": "unknown"},
])
def test_invalid_definition_is_rejected(tmp_path, overrides):
    with pytest.raises(ValidationError):
        load(tmp_path, [service(**overrides)])


def test_duplicate_names_are_rejected(tmp_path):
    with pytest.raises(ValidationError, match="unique"):
        load(tmp_path, [service(), service()])


def test_malformed_file_is_rejected(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text("{broken")
    with pytest.raises(ValidationError):
        load_services(path)


def _manifest_source(tmp_path):
    from palinreplay.core.models import Node
    from palinreplay.preprocess.source import pin_source
    from palinreplay.storage.store import RecordStore
    from tests.helpers import recorder_smoke as fake

    fake.main(tmp_path / "smoke")
    with RecordStore.open(tmp_path / "smoke/record") as store:
        source = pin_source(store).document()
        nodes = sorted(store.list(Node), key=lambda node: node.index)
        execution = nodes[1].tool_execution_ids[0]
    return tmp_path / "smoke/record", source, nodes[1].index, execution


def _manifest(tmp_path, **source_overrides):
    from palinreplay.preprocess.manifest import (
        EvidenceRef,
        ManifestEvidence,
        ManifestLaunch,
        ManifestService,
        ManifestSource,
        ServiceManifest,
    )

    record, source, index, execution = _manifest_source(tmp_path)
    source.update(source_overrides)
    manifest = ServiceManifest(
        schema_version="palinreplay.preprocess-services.v1",
        source=ManifestSource(**source),
        services=(ManifestService(
            name="A", start_inclusive=index, end_exclusive=index + 2,
            launch=ManifestLaunch(
                start_command="start A", readiness_command="probe A",
                cwd="/testbed", env={}, timeout_sec=30,
            ),
            evidence=ManifestEvidence(
                start=EvidenceRef(index=index, execution_id=execution),
                probe=EvidenceRef(index=index, execution_id=execution),
            ),
        ),),
    )
    return record, manifest


def test_load_service_input_supports_both_formats_and_rejects_versions(tmp_path):
    from palinreplay.restoration.service_config import load_service_input

    static = tmp_path / "static.json"
    static.write_text(json.dumps({"services": [service()]}))
    loaded = load_service_input(static)
    assert loaded.static.services[0].name == "A" and loaded.manifest is None

    _record, manifest = _manifest(tmp_path)
    path = tmp_path / "manifest.json"
    path.write_text(manifest.model_dump_json())
    loaded = load_service_input(path)
    assert loaded.manifest == manifest and loaded.static is None

    path.write_text(json.dumps({"schema_version": "other", "services": []}))
    with pytest.raises(Exception, match="schema_version"):
        load_service_input(path)


def test_resolve_manifest_services_at_replay_start(tmp_path):
    from palinreplay.replay.plan import pin_replay
    from palinreplay.restoration.service_config import ServiceInput, resolve_services
    from palinreplay.storage.store import RecordStore

    record, manifest = _manifest(tmp_path)
    with RecordStore.open(record) as store:
        plan = pin_replay(store, "C1", "C3")
        selected = resolve_services(ServiceInput(None, manifest, tmp_path / "m.json"), store, plan)
        assert [item.name for item in selected.services] == ["A"]
        assert selected.services[0].start_command == "start A"
        empty_plan = pin_replay(store, "C3", "C3")
        assert resolve_services(
            ServiceInput(None, manifest, tmp_path / "m.json"), store, empty_plan
        ).services == ()


def test_manifest_source_and_evidence_are_verified_before_runtime(tmp_path):
    from palinreplay.replay.plan import pin_replay
    from palinreplay.restoration.service_config import ServiceInput, resolve_services
    from palinreplay.storage.store import RecordStore

    record, manifest = _manifest(tmp_path, selection_sha256="0" * 64)
    with RecordStore.open(record) as store:
        with pytest.raises(Exception, match="source digest"):
            resolve_services(ServiceInput(None, manifest, tmp_path / "m.json"), store,
                             pin_replay(store, "C1", "C3"))

    record, manifest = _manifest(tmp_path / "other")
    with RecordStore.open(record) as store:
        evidence = manifest.services[0].evidence.model_copy(
            update={
                "start": manifest.services[0].evidence.start.model_copy(
                    update={"execution_id": "missing"}
                )
            }
        )
        service = manifest.services[0].model_copy(update={"evidence": evidence})
        bad = manifest.model_copy(update={"services": (service,)})
        with pytest.raises(Exception, match="evidence"):
            resolve_services(ServiceInput(None, bad, tmp_path / "m.json"), store,
                             pin_replay(store, "C1", "C3"))


def test_static_config_without_readiness_and_binary_manifest_resolve_without_probe(tmp_path):
    from palinreplay.preprocess.manifest import SCHEMA_VERSION, manifest_document
    from palinreplay.replay.plan import pin_replay
    from palinreplay.restoration.service_config import ServiceInput, resolve_services
    from palinreplay.storage.store import RecordStore

    assert load(tmp_path, [{"name": "A", "start_command": "start A", "timeout_sec": 10}]).services[0].readiness_command is None
    record, legacy = _manifest(tmp_path)
    document = legacy.model_dump(mode="json")
    document["schema_version"] = SCHEMA_VERSION
    document["services"][0]["launch"].pop("readiness_command")
    document["services"][0]["evidence"].pop("probe")
    binary = type(legacy).model_validate(document)
    assert "unknowns" not in manifest_document(binary)
    with RecordStore.open(record) as store:
        config = resolve_services(ServiceInput(None, binary, tmp_path / "m.json"), store,
                                  pin_replay(store, "C1", "C3"))
    assert config.services[0].readiness_command is None
