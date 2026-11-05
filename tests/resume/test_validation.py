from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from palinreplay.core.errors import IntegrityError
from palinreplay.resume import select_resume_start
from palinreplay.resume.materials import load_resume_materials
from palinreplay.resume.validation import validate_workspace
from palinreplay.storage.rootfs import SCOPE
from tests.record import test_restore_materials as fixtures

record = fixtures.record


@pytest.mark.parametrize("difference", [False, True])
def test_r1_compares_once_in_place_and_keeps_scope_and_failure(record, difference):
    path, (_, node, *_), _ = record
    materials = load_resume_materials(select_resume_start(path, node.node_id, model="M", max_steps=2))
    container = Mock()
    error = IntegrityError("restored filesystem differs: different_paths=['testbed/managed.py']")
    command = Mock(side_effect=error if difference else None, return_value={"equal": True})
    restored = SimpleNamespace(
        context=materials.context, config=materials.config, input_position=materials.input_position,
        report={"snapshot": materials.snapshot.model_dump(mode="json"), "checks": {}},
        _write_report=Mock(),
    )
    executor = SimpleNamespace(restored=restored, _container=container, runtime=materials.runtime,
                               pool=SimpleNamespace(command=command), workspace="workspaces/test")
    if difference:
        with pytest.raises(IntegrityError) as caught:
            validate_workspace(materials, executor)
        assert caught.value is error
        assert "managed.py" in error.r1_report["error"]
        assert restored.report["status"] == "failed"
    else:
        result = validate_workspace(materials, executor)
        assert result["status"] == "passed" and restored.report["status"] == "verified"
    command.assert_called_once_with(
        "verify", source=materials.snapshot.name, workspace="workspaces/test", scope=SCOPE,
        oracle_sha256=materials.runtime.c0_oracle_sha256,
    )
    container.pause.assert_called_once()
    container.unpause.assert_called_once()
    assert restored.report["r1"]["scope"] == SCOPE
    assert restored.report["r1"]["capture"] == materials.state.capture.model_dump(mode="json")
