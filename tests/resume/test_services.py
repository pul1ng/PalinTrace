import json
from types import SimpleNamespace

import pytest

import palinreplay.resume.services as services_module
import palinreplay.resume.session as session_module
from palinreplay.core.errors import IntegrityError, RecordUsageError
from palinreplay.resume import restore_resume_start, select_resume_start
from palinreplay.resume.services import load_resume_service_input, resolve_resume_services
from palinreplay.sandbox.base import ExecResult
from tests.replay.test_service_config import _manifest, service
from tests.resume import test_session

setup = test_session.setup


@pytest.fixture
def prepared(setup, tmp_path, monkeypatch):
    path = tmp_path / "services.json"
    path.write_text(json.dumps({"services": [
        service("A", readiness_command=None),
        service("B", readiness_command=None, cwd="/srv/B", env={"FLAG": "value"}),
    ]}))
    setup.executor.runtime = SimpleNamespace(launch=SimpleNamespace(workdir="/image"))
    monkeypatch.setattr(session_module, "saved_execution_config", lambda materials: {"cwd": "/saved"})
    events = []
    setup.validate.side_effect = lambda *args: events.append("r1") or {"status": "passed"}

    def execute(command, **kwargs):
        events.append(command)
        return ExecResult(command, b"started\xff\n", 0, 0.25)

    setup.executor.exec.side_effect = execute
    return SimpleNamespace(session=setup, path=path, events=events)


def test_optional_service_input_skips_reading():
    assert load_resume_service_input(None) is None


@pytest.mark.parametrize("version", ["static", "v1", "v2"])
def test_resume_reads_existing_service_formats(tmp_path, version):
    path = tmp_path / "services.json"
    if version == "static":
        document = {"services": [service()]}
    else:
        _, manifest = _manifest(tmp_path)
        document = manifest.model_dump(mode="json")
        document["schema_version"] = f"palinreplay.preprocess-services.{version}"
        if version == "v2":
            document["services"][0]["launch"].pop("readiness_command")
            document["services"][0]["evidence"].pop("probe")
    path.write_text(json.dumps(document))
    loaded = load_resume_service_input(path)
    if version == "static":
        assert loaded.static.services[0].name == "A" and loaded.manifest is None
    else:
        assert loaded.manifest.schema_version.endswith(version) and loaded.static is None
        assert loaded.manifest.services[0].name == "A"


@pytest.mark.parametrize("document", [
    "{broken", "[]", '{"schema_version":"unknown","services":[]}',
    '{"services":[{"name":"A","start_command":"launch","timeout_sec":0}]}',
])
def test_invalid_services_fail_before_container_creation(setup, tmp_path, document):
    path = tmp_path / "invalid.json"
    path.write_text(document)
    with pytest.raises(RecordUsageError):
        with restore_resume_start(setup.start, setup.output, services_config=path):
            pytest.fail("invalid services delivered")
    setup.connect.assert_not_called()
    setup.restore.assert_not_called()
    report = json.loads((setup.output / "report.json").read_text())
    assert report["caller"]["status"] == "not_started" and report["cleanup_confirmed"]


@pytest.mark.parametrize("index,expected", [(0, []), (1, ["A"]), (2, ["A"]), (3, [])])
def test_services_use_complete_single_node_boundary(tmp_path, monkeypatch, index, expected):
    record, manifest = _manifest(tmp_path)
    start = select_resume_start(record, f"C{index}", model="M", max_steps=2)
    path = tmp_path / "manifest.json"
    path.write_text(manifest.model_dump_json())
    original_pin = services_module.pin_replay
    boundaries = []

    def pin(store, first, last, **kwargs):
        boundaries.append((first, last, kwargs))
        plan = original_pin(store, first, last, **kwargs)
        assert plan.nodes == (start.node,)
        return plan

    monkeypatch.setattr(services_module, "pin_replay", pin)
    selected = resolve_resume_services(start, load_resume_service_input(path))
    assert [item.name for item in selected.services] == expected
    assert boundaries == [(start.node.node_id, start.node.node_id,
                           {"run_id": start.node.run_id})]


def test_no_services_and_static_configuration_remain_distinct(setup, tmp_path):
    assert resolve_resume_services(setup.start, None) is None
    path = tmp_path / "static.json"
    path.write_text(json.dumps({"services": [service("B"), service("A")]}))
    selected = resolve_resume_services(setup.start, load_resume_service_input(path))
    assert [item.name for item in selected.services] == ["B", "A"]


@pytest.mark.parametrize("fault,match", [
    ("run", "belongs to Run"), ("digest", "source digest"),
    ("range", "outside the manifest"), ("evidence", "evidence"),
])
def test_manifest_preflight_failures_precede_restoration(setup, tmp_path, fault, match):
    record, manifest = _manifest(tmp_path)
    start = select_resume_start(record, "C0" if fault == "range" else "C1",
                                model="M", max_steps=2)
    document = manifest.model_dump(mode="json")
    if fault == "run":
        document["source"]["run_id"] = "another-run"
    elif fault == "digest":
        document["source"]["selection_sha256"] = "0" * 64
    elif fault == "range":
        document["source"]["start_index"] = 1
    else:
        document["services"][0]["evidence"]["start"]["execution_id"] = "missing"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document))
    with pytest.raises(RecordUsageError, match=match):
        with restore_resume_start(start, setup.output, services_config=path):
            pytest.fail("invalid source delivered")
    setup.connect.assert_not_called()
    setup.restore.assert_not_called()


def test_services_prepare_after_r1_before_same_executor_is_delivered(prepared):
    setup = prepared.session
    with restore_resume_start(setup.start, setup.output, services_config=prepared.path) as session:
        assert prepared.events == ["r1", "start A", "start B"]
        assert session.executor is setup.executor and session.restored is setup.restored
        journal = json.loads((session.output / "preparation.json").read_text())
        assert journal["status"] == "ready"
        assert [entry["name"] for entry in journal["services"]] == ["A", "B"]
        assert all("readiness" not in entry and "final_readiness" not in entry
                   for entry in journal["services"])
        assert session.prefix.context is setup.materials.context
        assert session.start.model == "M" and session.start.max_steps == 2
    first, second = setup.executor.exec.call_args_list
    assert first.args == ("start A",) and second.args == ("start B",)
    assert first.kwargs["cwd"] == "/saved" and first.kwargs["env"] == {}
    assert second.kwargs["cwd"] == "/srv/B" and second.kwargs["env"] == {"FLAG": "value"}
    assert all(0 < call.kwargs["timeout_sec"] <= 10
               for call in setup.executor.exec.call_args_list)
    assert journal["services"][0]["startup"]["cwd"] == "/saved"
    setup.restore.assert_called_once()
    setup.executor.close.assert_called_once()
    setup.restored.close.assert_called_once()


def test_r1_failure_prevents_all_service_commands(prepared):
    setup = prepared.session
    setup.validate.side_effect = IntegrityError("R1 differs")
    with pytest.raises(IntegrityError):
        with restore_resume_start(setup.start, setup.output, services_config=prepared.path):
            pytest.fail("R1 failure delivered")
    setup.executor.exec.assert_not_called()
    assert not (setup.output / "preparation.json").exists()


@pytest.mark.parametrize("configured", [False, True])
def test_no_configuration_skips_but_empty_selection_prepares_zero_services(setup, tmp_path, configured):
    path = tmp_path / "empty.json"
    path.write_text('{"services":[]}')
    with restore_resume_start(setup.start, setup.output,
                              services_config=path if configured else None):
        preparation = setup.output / "preparation.json"
        assert preparation.exists() is configured
        if configured:
            saved = json.loads(preparation.read_text())
            assert saved["status"] == "ready" and saved["services"] == []
    setup.executor.exec.assert_not_called()


def test_explicit_legacy_readiness_uses_existing_preparation(prepared):
    setup = prepared.session
    prepared.path.write_text(json.dumps({"services": [service()]}))
    with restore_resume_start(setup.start, setup.output, services_config=prepared.path):
        assert prepared.events == ["r1", "start A", "probe A", "probe A"]
        saved = json.loads((setup.output / "preparation.json").read_text())
        entry, = saved["services"]
        assert len(entry["readiness"]) == 1
        assert entry["final_readiness"]["exit_code"] == 0
