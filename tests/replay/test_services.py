import base64
import json
from types import SimpleNamespace

import pytest

import palinreplay.replay.services as services
from palinreplay.replay.service_config import ServicesConfig, ServiceSpec
from palinreplay.replay.services import (
    PreparationError,
    PreparationJournal,
    prepare_services,
    start_service,
    wait_ready,
)
from palinreplay.sandbox.base import ExecResult, SandboxTimeout


def setup(tmp_path, **overrides):
    spec = ServiceSpec.model_validate({"name": "A", "start_command": "launch A",
                                       "readiness_command": "probe A", "timeout_sec": 10,
                                       **overrides})
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


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(services.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(services.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


@pytest.mark.parametrize("failure", [None, "wrong-response", "exited", "timeout", "late-success"])
def test_readiness_has_separate_attempts_and_shares_startup_deadline(tmp_path, clock, failure):
    spec, journal = setup(tmp_path, timeout_sec=0.35)
    calls = []

    def execute(command, **kwargs):
        calls.append((command, kwargs["timeout_sec"]))
        clock[0] += 0.1
        if command == spec.start_command:
            return ExecResult(command, b"started", 0, 0.1)
        if failure == "timeout":
            raise SandboxTimeout("probe discarded environment")
        if failure == "late-success":
            clock[0] = 1
        code = 2 if failure == "wrong-response" else 1 if failure == "exited" else 0
        if failure is None and len(calls) == 2:
            code = 1
            clock[0] -= 0.08
        return ExecResult(command, b"actual probe", code, 0.1)

    executor = SimpleNamespace(exec=execute, runtime=SimpleNamespace(launch=SimpleNamespace(workdir="/")))
    started = start_service(executor, spec, journal)
    if failure:
        with pytest.raises(SandboxTimeout if failure == "timeout" else PreparationError):
            wait_ready(executor, started, journal)
    else:
        wait_ready(executor, started, journal)
    saved = json.loads(journal.path.read_text())["services"][0]
    assert saved["status"] == ("failed" if failure else "ready")
    assert calls[0][0] == spec.start_command
    assert all(command == spec.readiness_command for command, _ in calls[1:])
    assert calls[1][1] < calls[0][1]
    assert len(saved["readiness"]) == len(calls) - 1
    assert saved["startup"]["output_base64"] != saved["readiness"][0].get("output_base64")
    if failure == "timeout":
        assert len(saved["readiness"]) == 1


@pytest.mark.parametrize("failed_b", [False, True])
def test_ordered_services_preserve_partial_evidence_and_do_not_start_c(tmp_path, failed_b):
    config = ServicesConfig(services=tuple(ServiceSpec(
        name=name, start_command="start " + name, readiness_command="ready " + name,
        timeout_sec=10,
    ) for name in ("A", "B")))
    journal = PreparationJournal(tmp_path / "preparation.json", config)
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        if command == "start B":
            assert journal.data["services"][0]["status"] == "ready"
        return ExecResult(command, command.encode(), 17 if failed_b and command == "start B" else 0, 0.1)

    executor = SimpleNamespace(exec=execute, runtime=SimpleNamespace(launch=SimpleNamespace(workdir="/")))
    if failed_b:
        with pytest.raises(PreparationError):
            prepare_services(executor, config, journal)
    else:
        prepare_services(executor, config, journal)
    saved = json.loads(journal.path.read_text())
    assert calls == ["start A", "ready A", "start B"] + ([] if failed_b else ["ready B"])
    assert saved["status"] == ("failed" if failed_b else "ready")
    assert [entry["status"] for entry in saved["services"]] == ["ready", "failed" if failed_b else "ready"]
    assert base64.b64decode(saved["services"][0]["startup"]["output_base64"]) == b"start A"
