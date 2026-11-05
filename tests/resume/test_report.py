import json

import pytest

import palinreplay.resume.report as report_module
import palinreplay.resume.session as session_module
from palinreplay.core.errors import IntegrityError
from palinreplay.resume import restore_resume_start
from palinreplay.sandbox.base import SandboxCleanupError
from tests.resume import test_session

setup = test_session.setup


def read(setup):
    return json.loads((setup.output / "report.json").read_text())


def test_preflight_failure_has_report_before_material_reads(setup, monkeypatch):
    original = IntegrityError("missing context artifact")

    def fail(start):
        assert read(setup)["phase"] == "materials"
        raise original

    monkeypatch.setattr(session_module, "load_resume_materials", fail)
    with pytest.raises(IntegrityError) as caught:
        with restore_resume_start(setup.start, setup.output):
            pytest.fail("bad materials delivered")
    assert caught.value is original
    report = read(setup)
    assert report["restore_status"] == "failed"
    assert report["restore_error"]["phase"] == "materials"
    assert report["caller"]["status"] == "not_started" and report["cleanup_confirmed"]
    assert original.resume_report == report
    setup.connect.assert_not_called()
    setup.restore.assert_not_called()


def test_caller_failure_is_independent_of_restoration(setup):
    original = KeyboardInterrupt("caller interrupted")
    with pytest.raises(KeyboardInterrupt) as caught:
        with restore_resume_start(setup.start, setup.output) as session:
            assert session.report["restore_status"] == "passed"
            raise original
    assert caught.value is original
    report = read(setup)
    assert report["status"] == "failed" and report["restore_status"] == "passed"
    assert report["caller"]["error_type"] == "KeyboardInterrupt"
    assert report["r1"]["status"] == "passed" and report["cleanup_confirmed"]
    assert session.report == report


@pytest.mark.parametrize("caller_error", [False, True])
def test_all_cleanup_attempts_run_and_keep_primary_exception(setup, caller_error):
    original = RuntimeError("caller")
    setup.executor.close.side_effect = OSError("executor close")
    setup.restored.close.side_effect = OSError("restore close")
    setup.connect.return_value.close.side_effect = OSError("client close")
    expected = RuntimeError if caller_error else SandboxCleanupError
    with pytest.raises(expected) as caught:
        with restore_resume_start(setup.start, setup.output):
            if caller_error:
                raise original
    if caller_error:
        assert caught.value is original and "cleanup also failed" in original.__notes__[0]
    report = read(setup)
    assert not report["cleanup_confirmed"] and report["status"] == "failed"
    assert {e["resource"] for e in report["cleanup_errors"]} == {"executor", "restore", "client"}
    setup.executor.close.assert_called_once()
    setup.restored.close.assert_called_once()
    setup.connect.return_value.close.assert_called_once()


def test_restore_construction_failure_reuses_lower_evidence(setup):
    error = IntegrityError("snapshot missing")
    error.restore_report = {"status": "failed", "cleanup_confirmed": True, "cleanup_errors": []}
    setup.restore.side_effect = error
    with pytest.raises(IntegrityError) as caught:
        with restore_resume_start(setup.start, setup.output):
            pytest.fail("failed restoration delivered")
    assert caught.value is error
    assert read(setup)["resources"] == error.restore_report
    assert read(setup)["cleanup"]["restore"] == "closed_by_restorer"


def test_r1_failure_evidence_survives_cleanup(setup):
    error = IntegrityError("managed path differs")
    error.r1_report = {"status": "failed", "error": str(error), "scope": {"id": "rootfs-files-v1"}}
    setup.validate.side_effect = error
    with pytest.raises(IntegrityError):
        with restore_resume_start(setup.start, setup.output):
            pytest.fail("bad R1 delivered")
    report = read(setup)
    assert report["r1"] == error.r1_report
    assert report["restore_error"]["phase"] == "validate_r1" and report["cleanup_confirmed"]


@pytest.mark.parametrize("caller_error", [False, True])
def test_final_report_failure_preserves_original_and_closes(setup, monkeypatch, caller_error):
    original = RuntimeError("caller")
    disk_error = OSError("disk full")
    with pytest.raises(RuntimeError if caller_error else OSError) as caught:
        with restore_resume_start(setup.start, setup.output):
            def fail(*args, **kwargs):
                raise disk_error
            monkeypatch.setattr(report_module, "write_json", fail)
            if caller_error:
                raise original
    assert caught.value is (original if caller_error else disk_error)
    assert caught.value.resume_report["cleanup_confirmed"]
    assert caught.value.resume_report["report_error"]["error"] == "disk full"
    setup.restored.close.assert_called_once()
    setup.connect.return_value.close.assert_called_once()
