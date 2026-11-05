import json

import pytest

from palinreplay.core.errors import RecordUsageError
from palinreplay.resume import restore_resume_start
from palinreplay.resume.services import load_resume_service_input
from tests.replay.test_service_config import _manifest, service
from tests.resume import test_session

setup = test_session.setup


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
