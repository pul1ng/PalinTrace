from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import palinreplay.resume.session as session_module
from palinreplay.core.errors import IntegrityError, RecordUsageError
from palinreplay.resume import ResumeSession, restore_resume_start


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = tmp_path / "recording" / "record"
    source.mkdir(parents=True)
    start = SimpleNamespace(source_store=source, node=SimpleNamespace(index=20))
    materials = SimpleNamespace(context={"messages": ["saved"]}, input_position={"next_message": 1})
    restored = Mock(report={})
    executor = Mock()
    executor.exec.return_value = SimpleNamespace(text="/testbed\n", exit_code=0)
    connect = Mock()
    restore = Mock(return_value=(restored, executor))
    validate = Mock(return_value={"status": "passed"})
    monkeypatch.setattr(session_module, "load_resume_materials", lambda start: materials)
    monkeypatch.setattr(session_module, "validate_resume_context", session_module.ResumePrefix)
    monkeypatch.setattr(session_module, "saved_execution_config", lambda materials: {})
    monkeypatch.setattr(session_module, "connect_docker", connect)
    monkeypatch.setattr(session_module, "restore_environment", restore)
    monkeypatch.setattr(session_module, "validate_workspace", validate)
    return SimpleNamespace(start=start, materials=materials, restored=restored, executor=executor,
                           connect=connect, restore=restore, validate=validate, output=tmp_path / "out")


@pytest.mark.parametrize("provided_client", [False, True])
@pytest.mark.parametrize("caller_error", [False, True])
def test_session_is_validated_before_delivery_and_closes(setup, provided_client, caller_error):
    client = Mock() if provided_client else None
    original = RuntimeError("caller failed")
    try:
        with restore_resume_start(setup.start, setup.output, client) as session:
            assert isinstance(session, ResumeSession)
            setup.validate.assert_called_once_with(setup.materials, setup.executor)
            assert session.executor is setup.executor and session.restored is setup.restored
            assert session.context is setup.materials.context
            assert session.input_position is setup.materials.input_position
            assert session.executor.exec("pwd").text == "/testbed\n"
            if caller_error:
                raise original
    except RuntimeError as error:
        assert caller_error and error is original
    setup.executor.close.assert_called_once()
    setup.restored.close.assert_called_once()
    if provided_client:
        client.close.assert_not_called()
        setup.connect.assert_not_called()
    else:
        setup.connect.return_value.close.assert_called_once()


def test_r1_failure_never_delivers_and_still_closes(setup):
    setup.validate.side_effect = IntegrityError("different workspace")
    with pytest.raises(IntegrityError):
        with restore_resume_start(setup.start, setup.output):
            pytest.fail("unverified session delivered")
    setup.executor.close.assert_called_once()
    setup.restored.close.assert_called_once()
    setup.connect.return_value.close.assert_called_once()


@pytest.mark.parametrize("location", ["record", "recording", "link", "existing"])
def test_output_must_be_new_and_outside_entire_recording(setup, tmp_path, location):
    if location == "record":
        output = setup.start.source_store / "output"
    elif location == "recording":
        output = setup.start.source_store.parent / "output"
    elif location == "link":
        link = tmp_path / "link"
        link.symlink_to(setup.start.source_store.parent, target_is_directory=True)
        output = link / "output"
    else:
        output = setup.output
        output.mkdir()
    with pytest.raises((RecordUsageError, FileExistsError)):
        with restore_resume_start(setup.start, output):
            pytest.fail("unsafe output accepted")
    setup.restore.assert_not_called()


def test_default_output_is_in_current_directory(setup, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with restore_resume_start(setup.start) as session:
        assert session.output.parent == tmp_path / "outputs" / "resume"
        assert session.output.name.endswith("_C20-restore")
