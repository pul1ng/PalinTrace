import base64
import json
from types import SimpleNamespace

import pytest

from palinreplay.replay.service_config import ServicesConfig, ServiceSpec
from palinreplay.replay.services import PreparationError, PreparationJournal, start_service
from palinreplay.sandbox.base import ExecResult, SandboxTimeout


def setup(tmp_path, **overrides):
    spec = ServiceSpec(name="A", start_command="launch A", readiness_command="probe A",
                       timeout_sec=10, **overrides)
    journal = PreparationJournal(tmp_path / "preparation.json", ServicesConfig(services=(spec,)))
    return spec, journal


@pytest.mark.parametrize("overrides", [{}, {"cwd": "/srv/A", "env": {"FLAG": "value"}}])
def test_startup_uses_executor_once_and_preserves_raw_evidence(tmp_path, overrides):
    spec, journal = setup(tmp_path, **overrides)
    calls = []
    raw = b"\xff\x00line\n\n"

    def execute(command, **kwargs):
        calls.append((command, kwargs))
        assert json.loads(journal.path.read_text())["services"][0]["startup"]["status"] == "running"
        return ExecResult(command, raw, 0, 0.25)

    executor = SimpleNamespace(exec=execute, runtime=SimpleNamespace(launch=SimpleNamespace(workdir="/testbed")))
    result = start_service(executor, spec, journal)
    assert len(calls) == 1
    command, settings = calls[0]
    assert command == spec.start_command and settings["cwd"] == spec.cwd and settings["env"] == spec.env
    assert 0 < settings["timeout_sec"] <= 10
    saved = json.loads(journal.path.read_text())
    assert saved["status"] == "preparing" and saved["services"][0]["status"] == "started"
    attempt = saved["services"][0]["startup"]
    assert base64.b64decode(attempt["output_base64"]) == raw
    assert attempt["exit_code"] == 0 and attempt["duration_sec"] == 0.25
    assert attempt["cwd"] == (spec.cwd or "/testbed")
    assert result.entry == saved["services"][0]


@pytest.mark.parametrize("fault", ["exit", "timeout"])
def test_startup_failure_is_not_retried_and_is_retained(tmp_path, fault):
    spec, journal = setup(tmp_path)
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        if fault == "timeout":
            raise SandboxTimeout("discarded")
        return ExecResult(command, b"\xfffailed\n", 17, 0.5)

    executor = SimpleNamespace(exec=execute, runtime=SimpleNamespace(launch=SimpleNamespace(workdir="/")))
    with pytest.raises(SandboxTimeout if fault == "timeout" else PreparationError):
        start_service(executor, spec, journal)
    assert calls == [spec.start_command]
    saved = json.loads(journal.path.read_text())
    assert saved["status"] == saved["services"][0]["status"] == "failed"
    attempt = saved["services"][0]["startup"]
    if fault == "exit":
        assert attempt["exit_code"] == 17 and base64.b64decode(attempt["output_base64"]) == b"\xfffailed\n"
    else:
        assert attempt["status"] == "unknown" and "exit_code" not in attempt
