import json
from types import SimpleNamespace

import pytest

from examples import resume_service_validation as validation
from palinreplay.core.errors import RecordUsageError
from palinreplay.llm import OpenAIChatClient
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.replay.service_config import ServicesConfig, ServiceSpec
from palinreplay.replay.services import PreparationJournal
from palinreplay.sandbox.base import ExecResult
from palinreplay.storage.store import RecordStore


def executor():
    result = object.__new__(RestoredExecutor)
    result._container = SimpleNamespace(id="same-container")
    result._workdir = "/testbed"
    result._timeout = 30
    result.runtime = SimpleNamespace(launch=SimpleNamespace(workdir="/testbed"))
    return result


def counts():
    return dict.fromkeys((*validation.FORBIDDEN, "startup_commands", "inspection_commands", "request_commands"), 0)


@pytest.mark.parametrize("location", ["store", "recording", "symlink"])
def test_service_acceptance_cannot_write_into_source(tmp_path, location):
    source = tmp_path / "recording" / "record"
    with RecordStore.create(source):
        pass
    output = source / "out" if location == "store" else source.parent / "out"
    if location == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(source.parent, target_is_directory=True)
        output = alias / "out"
    before = validation.restoration.file_inventory(source.parent)
    with pytest.raises(RecordUsageError):
        validation.main(source, tmp_path / "missing-services.json", output)
    assert not output.exists()
    assert validation.restoration.file_inventory(source.parent) == before


def test_guard_allows_and_counts_startups_and_inspections_but_rejects_new_work(tmp_path, monkeypatch):
    monkeypatch.setattr(RestoredExecutor, "exec",
                        lambda self, command, **kwargs: ExecResult(command, b"OK\n", 0, 0.25))
    actual_counts, actions = counts(), []
    container = executor()
    config = ServicesConfig(services=tuple(
        ServiceSpec(name=name, start_command="start " + name, timeout_sec=10)
        for name in ("A", "B")))
    journal = PreparationJournal(tmp_path / "preparation.json", config)
    with validation.no_new_work(actual_counts, actions):
        validation.session_module.prepare_services(container, config, journal)
        assert validation.check(container, "pwd", "pwd") == "OK\n"
        for call in (lambda: OpenAIChatClient.complete(None),
                     lambda: container.exec("unapproved history"),
                     lambda: RecordStore.commit_node(None, None)):
            with pytest.raises(AssertionError, match="attempted"):
                call()
    assert [action["category"] for action in actions] == ["startup", "startup", "inspection"]
    assert [action["service"] for action in actions[:2]] == ["A", "B"]
    assert all(action["container_id"] == "same-container" for action in actions)
    assert all(action["exit_code"] == 0 and action["duration_sec"] == 0.25 for action in actions)
    assert actual_counts["startup_commands"] == 2 and actual_counts["inspection_commands"] == 1
    assert actual_counts["model_requests"] == actual_counts["historical_tool_reexecutions"] == 1
    assert actual_counts["branch_node_commits"] == 1


@pytest.mark.parametrize("fault", ["exit", "exception"])
def test_failed_inspection_retains_actual_command_outcome(monkeypatch, fault):
    def fail(self, command, **kwargs):
        if fault == "exception":
            raise OSError("execution failed")
        return ExecResult(command, b"failed\n", 17, 0.5)

    monkeypatch.setattr(RestoredExecutor, "exec", fail)
    actual_counts, actions = counts(), []
    with validation.no_new_work(actual_counts, actions):
        with pytest.raises(OSError if fault == "exception" else AssertionError):
            validation.check(executor(), "failed-check", "check command")
    action, = actions
    assert action["command"] == "check command" and action["label"] == "failed-check"
    assert action["container_id"] == "same-container" and action["duration_sec"] >= 0
    if fault == "exception":
        assert action["error_type"] == "OSError" and "exit_code" not in action
    else:
        assert action["exit_code"] == 17 and action["output"] == "failed\n"
    assert actual_counts["inspection_commands"] == 1


def test_guard_rejects_unsupplied_service_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(RestoredExecutor, "exec",
                        lambda self, command, **kwargs: pytest.fail("unexpected command executed"))
    monkeypatch.setattr(validation.session_module, "prepare_services",
                        lambda executor, config, journal: executor.exec("unsupplied probe"))
    config = ServicesConfig(services=(ServiceSpec(name="A", start_command="start A", timeout_sec=10),))
    actual_counts, actions = counts(), []
    journal = PreparationJournal(tmp_path / "preparation.json", config)
    with validation.no_new_work(actual_counts, actions):
        with pytest.raises(AssertionError, match="unapproved_commands"):
            validation.session_module.prepare_services(executor(), config, journal)
    assert actual_counts["unapproved_commands"] == 1
    assert actual_counts["startup_commands"] == 0 and actions == []


@pytest.mark.parametrize("group,order", [("C20", ["A", "B"] * 3), ("C35", ["C"] * 3)])
def test_explicit_requests_record_every_response_and_count_separately(monkeypatch, group, order):
    def respond(self, command, **kwargs):
        name = command.split("fixture/")[1][0]
        output = json.dumps({"status": 200, "body": f"PALIN_SERVICE_{name}_OK"}).encode()
        return ExecResult(command, output, 0, 0.125)

    monkeypatch.setattr(RestoredExecutor, "exec", respond)
    actual_counts, actions = counts(), []
    token = validation._GROUP.set(group)
    try:
        with validation.no_new_work(actual_counts, actions):
            validation.request_services(executor(), actions, order)
    finally:
        validation._GROUP.reset(token)
    assert validation.validate_requests(actions, group, order) == {
        name: order.count(name) for name in ("A", "B", "C")}
    assert [action["service"] for action in actions] == order
    assert [action["label"] for action in actions] == (
        ["A1", "B1", "A2", "B2", "A3", "B3"] if group == "C20" else ["C1", "C2", "C3"])
    assert actual_counts["request_commands"] == len(order)
    assert actual_counts["inspection_commands"] == actual_counts["startup_commands"] == 0
    assert all(action["http_status"] == 200 and action["duration_sec"] == 0.125 for action in actions)


@pytest.mark.parametrize("status,body,exit_code", [
    (500, "PALIN_SERVICE_A_OK", 0), (200, "wrong body", 0), (200, "PALIN_SERVICE_A_OK", 2),
])
def test_bad_request_preserves_response_and_fails_without_retry(monkeypatch, status, body, exit_code):
    output = json.dumps({"status": status, "body": body}).encode()
    monkeypatch.setattr(RestoredExecutor, "exec",
                        lambda self, command, **kwargs: ExecResult(command, output, exit_code, 0.5))
    actual_counts, actions = counts(), []
    with validation.no_new_work(actual_counts, actions):
        with pytest.raises(AssertionError):
            validation.request_services(executor(), actions, ["A", "A", "A"])
    action, = actions
    assert action["http_status"] == status and action["body"] == body
    assert action["exit_code"] == exit_code and not action["request_passed"]
    assert actual_counts["request_commands"] == 1


@pytest.mark.parametrize("fault", ["missing", "http", "body", "exit", "attempt", "failed"])
def test_request_summary_rejects_any_incomplete_or_unsuccessful_request(fault):
    actions = [{"group": "C20", "category": "request", "service": name, "attempt": attempt,
                "http_status": 200, "body": f"PALIN_SERVICE_{name}_OK", "exit_code": 0,
                "duration_sec": 0.1, "request_passed": True}
               for attempt in (1, 2, 3) for name in ("A", "B")]
    if fault == "missing":
        actions.pop()
    else:
        field, value = {"http": ("http_status", 500), "body": ("body", "wrong"),
                        "exit": ("exit_code", 1), "attempt": ("attempt", 2),
                        "failed": ("request_passed", False)}[fault]
        actions[0][field] = value
    with pytest.raises(AssertionError):
        validation.validate_requests(actions, "C20", ["A", "B"] * 3)
