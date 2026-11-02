import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

import palinreplay.replay.session as session
from palinreplay.agents import AgentTask
from palinreplay.core.models import Node, State
from palinreplay.record.calls import read_call
from palinreplay.replay.materials import load_replay_materials
from palinreplay.replay.plan import pin_replay
from palinreplay.replay.session import run_replay
from palinreplay.sandbox.base import ExecResult, SandboxTimeout
from palinreplay.sandbox.runtime import C0LaunchConfig
from palinreplay.storage.btrfs import verification
from palinreplay.storage.btrfs.models import BACKEND, BtrfsRuntime, PoolLocation, Snapshot
from palinreplay.storage.store import RecordStore
from tests.agents.test_mini_agent import Sandbox, deterministic, integrated, response  # noqa: F401
from tests.helpers import recorder_smoke as fake


def test_preflight_failure_retains_report_without_starting_docker(tmp_path, monkeypatch):
    source = tmp_path / "source"
    with RecordStore.create(source) as store:
        agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
        with fake.recorder(store, agent, backend) as rec:
            rec.finish()
    def forbidden(*args):
        raise AssertionError("Docker must not start before material preflight")
    monkeypatch.setattr("palinreplay.replay.session.connect_docker", forbidden)
    output = tmp_path / "output"
    report = run_replay(source, "C0", output=output)
    assert report["status"] == "failed" and report["phase"] == "preflight"
    assert report["execution_status"] == "failed"
    assert report["validation_status"] == "incomplete"
    assert report["validation_mode"] == "lenient"
    assert "version" in report["error"]
    assert report["cleanup_confirmed"]
    assert json.loads((output / "report.json").read_text()) == report


@pytest.fixture
def replay_session(tmp_path, monkeypatch):
    """Real history, session and recorder, with only the external Btrfs boundary replaced."""
    state = SimpleNamespace(fault=None, different_call=None, file_difference=False,
                            cleanup_fault=False, termination_fault=False, closed=[],
                            comparisons=[], captures=0, restored=False, recorder=None,
                            preparation_difference=False)
    identity = uuid4()
    location = PoolLocation(volume="palinreplay-btrfs-" + identity.hex, uuid=identity,
                            daemon_id="fixture", helper_image_id="sha256:" + "a" * 64)
    launch = C0LaunchConfig(platform="linux/amd64", environment={"PATH": "/bin"},
                           omitted_environment_keys=(), workdir="/testbed", user="",
                           shell=("bash", "-c"), entrypoint=(), command=(), network_mode="none",
                           memory_bytes=0, nano_cpus=0)
    runtime = BtrfsRuntime(source_container_id="source", source_image_id="source-image",
                           bootstrap_image_id="bootstrap", launch=launch, c0_oracle_sha256="oracle")
    monkeypatch.setattr(verification, "verify_state", lambda *args: None)
    source, _, _, _, original = integrated(
        tmp_path, [response("read", "read"), response("read"), response("read"), response("submit")]
    )

    def capture(store, boundary, previous):
        snapshot = Snapshot(location=location, name="checkpoints/" + boundary.node_id, uuid=uuid4())
        manifest = store.put_json(snapshot.model_dump(mode="json"))
        return State(backend=BACKEND, capture=verification.capture_info(), manifest=manifest,
                     runtime=store.put_json(runtime.model_dump(mode="json")), artifacts=(manifest,),
                     logical_parent_id=previous.state_id if previous else None)

    original._capture_state = capture
    original.run(AgentTask("task", "Task"))
    state.source = source.root
    state.config = original.recorder.run.config
    source.close()
    sandbox = Sandbox()
    state.sandbox = sandbox

    class Pool:
        def __init__(self):
            self.location = location

        def verify(self, snapshot):
            if state.fault == "snapshot-read":
                raise RuntimeError("snapshot read fault")
            assert snapshot.location == location

        def snapshot(self, workspace, name):
            state.captures += 1
            if state.fault == "snapshot-capture" and state.captures == 3:
                raise RuntimeError("snapshot capture fault")
            return {"uuid": str(uuid4())}

        def command(self, op, **kwargs):
            if op == "verify":
                assert state.captures == 0, "full R1 check must precede initial capture"
            if op == "verify" and state.fault == "baseline":
                raise RuntimeError("baseline verification fault")
            assert op in ("verify", "usage")
            return {"equal": True}

        def changes(self, before, after):
            assert state.captures == 1, "compare preparation only after actual C0 exists"
            return {"complete": True, "candidates": ["service.pid"]}

        def _file_command(self, op, **kwargs):
            assert op == "compare_candidates"
            return {"equal": not state.preparation_difference,
                    "different_paths": ["service.pid"] if state.preparation_difference else []}

        def compare_incremental(self, *snapshots):
            index = len(state.comparisons)
            state.comparisons.append(snapshots)
            if state.fault == "compare" or (state.fault == "compare-after-file" and index == 1):
                raise RuntimeError("comparison fault")
            return {"equal": not (state.file_difference and index == 0),
                    "different_paths": ["file"] if state.file_difference and index == 0 else []}

    pool = Pool()
    container = SimpleNamespace(id="actual", attrs={"Config": {"Env": ["PATH=/bin"]}},
                                pause=lambda: None, unpause=lambda: None, reload=lambda: None)

    def close_restore():
        state.closed.append("restore")
        if state.cleanup_fault:
            raise RuntimeError("restore cleanup fault")

    restored = SimpleNamespace(pool=pool, close=close_restore,
                               _write_report=lambda: None,
                               report={"checks": {}, "snapshot": Snapshot(
                                   location=location, name="bases/C0", uuid=uuid4()
                               ).model_dump(mode="json")})
    executor = SimpleNamespace(runtime=runtime, pool=pool, workspace="workspaces/fixture",
                               _container=container, check_root=lambda: None, restored=restored)

    def execute(request):
        result = sandbox.exec(request["action"]["command"])
        if state.fault == "tool" and len(sandbox.calls) == 2:
            raise SandboxTimeout("unknown execution fault")
        if state.fault == "unknown" and len(sandbox.calls) == 2:
            return ExecResult(result.command, result.output, None, 1)
        if len(sandbox.calls) == state.different_call:
            return ExecResult(result.command, b"actual", 0, 1)
        return result

    state.executor = executor
    executor.execute = execute
    client = SimpleNamespace(close=lambda: state.closed.append("client"))
    state.client = client

    def restore(store, selector, **kwargs):
        saved = session.load_restore_materials(store, selector)
        restored.context = saved.context
        restored.input_position = saved.input_position
        restored.config = saved.config
        if state.fault == "context":
            restored.context = {"messages": ["future context"]}
        state.restored = True
        if state.fault == "restore":
            state.closed.append("restore-internal")
            error = RuntimeError("restore fault")
            error.restore_report = {"cleanup_confirmed": not state.cleanup_fault,
                                    "cleanup_errors": ["pool cleanup fault"] if state.cleanup_fault else []}
            raise error
        return restored

    initial = session.initial_recorder

    def initial_recorder(*args, **kwargs):
        state.recorder = initial(*args, **kwargs)
        if state.termination_fault:
            def fail_termination(*args):
                raise RuntimeError("termination fault")
            state.recorder.__exit__ = fail_termination
        return state.recorder

    monkeypatch.setattr(session, "connect_docker", lambda *args: client)
    monkeypatch.setattr(session, "docker_endpoint", lambda: "unix:///fixture.sock")
    monkeypatch.setattr(session, "restore_r1_node", restore)
    monkeypatch.setattr(session, "RestoredExecutor", lambda *args: executor)
    monkeypatch.setattr(session, "initial_recorder", initial_recorder)
    return state


@pytest.mark.parametrize("difference", [False, True])
def test_services_prepare_after_r1_before_c0_and_leave_history_unchanged(tmp_path, replay_session, difference):
    state = replay_session
    state.preparation_difference = difference
    commands = []
    spec = {"name": "A", "start_command": "start A", "readiness_command": "ready A",
            "timeout_sec": 10, "cwd": "/srv/A", "env": {"ONLY_SERVICE": "1"}}
    config = tmp_path / "services.json"
    config.write_text(json.dumps({"services": [spec]}))

    def execute(command, **kwargs):
        assert state.executor.restored.report["checks"]["full_filesystem"]
        assert state.captures == 0 and not state.sandbox.calls
        assert kwargs["cwd"] == spec["cwd"] and kwargs["env"] == spec["env"]
        commands.append(command)
        return ExecResult(command, b"real service result", 0, 0.1)

    state.executor.exec = execute
    output = tmp_path / "output"
    events = []
    report = run_replay(state.source, "C0", "C3", output=output,
                        services_config=config, progress=events.append)
    assert commands == ["start A", "ready A", "ready A"]
    assert report["r1"]["status"] == "passed" and report["preparation"]["status"] == "ready"
    assert report["execution_status"] == "completed"
    assert report["validation_status"] == ("difference" if difference else "match")
    assert all(step["starting_divergent"] == difference for step in report["steps"])
    assert json.loads((output / "preparation.json").read_text()) == report["preparation"]
    assert json.loads((output / "preparation-files.json").read_text()) == report["preparation_files"]
    assert [event.phase for event in events].index("service-preparation") < [event.phase for event in events].index("committed")
    assert [event.step for event in events if event.phase == "committed"] == [0, 1, 2, 3]
    assert report["statistics"]["actual_model_calls"] == 0
    assert report["statistics"]["actual_tool_executions"] == 4
    with RecordStore.open(state.source) as origin, RecordStore.open(output / "record") as actual:
        history = load_replay_materials(origin, pin_replay(origin, "C0", "C3"))
        for step, mapping in zip(history.steps, report["mappings"][1:], strict=True):
            node = actual.get(Node, mapping["node_id"])
            for tool, execution_id in zip(step.tools, node.tool_execution_ids, strict=True):
                trace = read_call(actual, execution_id)
                assert actual.read_json(trace.execution.request) == tool.request
        assert actual.read_json(actual.get(Node, report["mappings"][0]["node_id"]).context)["messages"] == history.context["messages"]
    assert state.executor.runtime.launch.workdir == "/testbed"
    assert state.executor.runtime.launch.environment == {"PATH": "/bin"}
    assert report["cleanup_confirmed"]


@pytest.mark.parametrize("fault", ["exit", "timeout", "cancel"])
def test_final_readiness_failure_blocks_c0_after_all_initial_checks_pass(tmp_path, replay_session, fault):
    state = replay_session
    config = tmp_path / "services.json"
    config.write_text(json.dumps({"services": [
        {"name": name, "start_command": "start " + name, "readiness_command": "ready " + name,
         "timeout_sec": 10} for name in ("A", "B")
    ]}))
    commands = []

    def execute(command, **kwargs):
        commands.append(command)
        if command == "ready A" and commands.count(command) == 2:
            if fault == "timeout":
                raise SandboxTimeout("A final check timed out")
            if fault == "cancel":
                raise KeyboardInterrupt("cancelled during final readiness")
            return ExecResult(command, b"A exited while B started", 1, 0.1)
        return ExecResult(command, b"initially ready", 0, 0.1)

    state.executor.exec = execute
    output = tmp_path / "output"
    if fault == "cancel":
        with pytest.raises(KeyboardInterrupt):
            run_replay(state.source, "C0", "C3", output=output, services_config=config)
        report = json.loads((output / "report.json").read_text())
    else:
        report = run_replay(state.source, "C0", "C3", output=output, services_config=config)
    assert commands == ["start A", "ready A", "start B", "ready B", "ready A"]
    assert report["execution_status"] == "failed" and report["phase"] == "service-preparation"
    assert report["preparation"]["status"] == "failed"
    saved = json.loads((output / "preparation.json").read_text())
    assert saved == report["preparation"]
    a, b = saved["services"]
    assert a["readiness"][0]["exit_code"] == b["readiness"][0]["exit_code"] == 0
    assert a["status"] == "failed" and b["status"] == "ready"
    assert a["final_readiness"]["status"] == ("returned" if fault == "exit" else "unknown")
    assert not (output / "record").exists() and state.captures == 0
    assert not state.sandbox.calls and state.recorder is None
    assert report["statistics"]["actual_tool_executions"] == 0
    assert report["cleanup_confirmed"] and state.closed == ["restore", "client"]


@pytest.mark.parametrize("cleanup_fault", [False, True])
@pytest.mark.parametrize("fault", ["startup", "readiness", "partial", "cancel", "cancel-before"])
def test_preparation_failure_blocks_c0_and_history_and_keeps_primary_error(
    tmp_path, replay_session, cleanup_fault, fault,
):
    state = replay_session
    state.cleanup_fault = cleanup_fault
    config = tmp_path / "services.json"
    config.write_text(json.dumps({"services": [
        {"name": name, "start_command": "start " + name, "readiness_command": "ready " + name,
         "timeout_sec": 10} for name in ("A", "B")
    ]}))
    commands = []

    def execute(command, **kwargs):
        commands.append(command)
        if fault == "readiness" and command == "ready A":
            raise SandboxTimeout("readiness command timed out; environment discarded")
        if fault == "cancel" and command == "start B":
            raise KeyboardInterrupt("cancelled during B")
        failed = (fault == "startup" and command == "start A") or (fault == "partial" and command == "start B")
        return ExecResult(command, b"\xffactual output", 17 if failed else 0, 0.1)

    def progress(event):
        if fault == "cancel-before" and event.phase == "service-preparation":
            raise KeyboardInterrupt("cancelled before commands")

    state.executor.exec = execute
    output = tmp_path / "output"
    if fault.startswith("cancel"):
        with pytest.raises(KeyboardInterrupt):
            run_replay(state.source, "C0", "C3", output=output,
                       services_config=config, progress=progress)
        report = json.loads((output / "report.json").read_text())
    else:
        report = run_replay(state.source, "C0", "C3", output=output,
                            services_config=config, progress=progress)
    assert report["execution_status"] == "failed" and report["phase"] == "service-preparation"
    assert report["r1"]["status"] == "passed"
    assert report["error_type"] == ("KeyboardInterrupt" if fault.startswith("cancel") else
                                    "SandboxTimeout" if fault == "readiness" else "PreparationError")
    assert report["error"] != "resource cleanup failed"
    assert not (output / "record").exists() and state.captures == 0
    assert not state.sandbox.calls and state.recorder is None
    assert report["statistics"]["actual_tool_executions"] == 0
    saved = json.loads((output / "preparation.json").read_text())
    assert saved == report["preparation"] and saved["status"] == "failed"
    if fault in ("partial", "cancel"):
        assert [service["status"] for service in saved["services"]] == ["ready", "failed"]
        assert saved["services"][0]["startup"]["exit_code"] == 0
        assert saved["services"][0]["readiness"][0]["exit_code"] == 0
    assert len(commands) == {"startup": 1, "readiness": 2, "partial": 3, "cancel": 3, "cancel-before": 0}[fault]
    assert report["cleanup_confirmed"] is (not cleanup_fault)
    assert state.closed == ["restore", "client"]
    assert bool(report["cleanup_errors"]) is cleanup_fault


@pytest.mark.parametrize("fault", ["baseline", "context"])
def test_failed_r1_never_runs_configured_service(tmp_path, replay_session, fault):
    state = replay_session
    state.fault = fault
    config = tmp_path / "services.json"
    config.write_text(json.dumps({"services": [{"name": "A", "start_command": "start A",
                                              "readiness_command": "ready A", "timeout_sec": 10}]}))

    def forbidden(*args, **kwargs):
        pytest.fail("service commands must not run after failed R1")

    state.executor.exec = forbidden
    output = tmp_path / "output"
    report = run_replay(state.source, "C0", "C3", output=output, services_config=config)
    assert report["r1"]["status"] == "failed" and not state.sandbox.calls
    assert not (output / "preparation.json").exists() and not (output / "record").exists()
    assert report["cleanup_confirmed"]


def test_invalid_service_config_is_rejected_before_source_or_output(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid service configuration must be rejected before opening resources")

    monkeypatch.setattr(session.RecordStore, "open", forbidden)
    monkeypatch.setattr(session, "connect_docker", forbidden)
    config = tmp_path / "services.json"
    config.write_text('{"services": [{"name": "A"}]}')
    output = tmp_path / "output"
    with pytest.raises(ValueError):
        run_replay(tmp_path / "missing-source", "C0", services_config=config, output=output)
    assert not output.exists()


@pytest.mark.parametrize("validation_mode", [None, "strict", "lenient"])
def test_session_mode_reaches_report_recording_and_loop(tmp_path, replay_session, validation_mode):
    state = replay_session
    state.different_call = 1
    options = {} if validation_mode is None else {"validation_mode": validation_mode}
    output = tmp_path / "output"
    events = []
    report = run_replay(state.source, "C0", "C3", output=output, progress=events.append, **options)
    strict = validation_mode == "strict"
    assert report["validation_mode"] == (validation_mode or "lenient")
    assert "continue_on_difference" not in report
    assert report["execution_status"] == ("stopped" if strict else "completed")
    assert report["validation_status"] == report["status"] == "difference"
    assert len(state.sandbox.calls) == (1 if strict else 4)
    with RecordStore.open(output / "record") as store:
        config = store.read_json(state.recorder.run.config)["replay"]
        assert config["validation_mode"] == report["validation_mode"]
        assert "continue_on_difference" not in config
        assert len(store.list(Node)) == (1 if strict else 4)
    assert report["cleanup_confirmed"] and state.closed == ["restore", "client"]
    assert state.recorder.phase == "closed"
    assert json.loads((output / "report.json").read_text()) == report
    assert [event.total_steps for event in events if event.total_steps is not None] == [3, 3]
    committed = [event.step for event in events if event.phase == "committed"]
    assert committed == ([0] if strict else [0, 1, 2, 3])
    assert events[0].phase == "preparing" and events[-2].phase == "cleanup"
    assert events[-1].status == report["execution_status"]
    assert events[-1].label == f"{report['execution_status'].upper()} / DIFFERENCE"


@pytest.mark.parametrize("start,end,expected", [("C1", "C3", [(1, 0), (2, 1), (3, 2)]),
                                              ("C2", "C2", [(2, 0)])])
def test_progress_counts_exclude_restored_baseline(tmp_path, replay_session, start, end, expected):
    events = []
    report = run_replay(replay_session.source, start, end, output=tmp_path / "output",
                        progress=events.append)
    assert report["execution_status"] == "completed" and report["validation_status"] == "match"
    assert [(event.source_step, event.step) for event in events if event.phase == "committed"] == expected
    assert all(event.total_steps == len(expected) - 1
               for event in events if event.total_steps is not None)
    assert [event.phase for event in events if event.step == 1] == (
        ["replaying", "verifying", "committed"] if len(expected) > 1 else []
    )
    if len(expected) == 1:
        assert not any(event.phase == "replaying" for event in events)


def test_progress_observer_error_does_not_change_execution(tmp_path, replay_session):
    observed = []

    def broken(event):
        observed.append(event)
        raise OSError("terminal disconnected")

    report = run_replay(replay_session.source, "C0", "C3", output=tmp_path / "output", progress=broken)
    assert len(observed) == 1
    assert report["execution_status"] == "completed" and report["validation_status"] == "match"
    assert len(replay_session.sandbox.calls) == 4 and report["cleanup_confirmed"]


def test_invalid_validation_mode_is_rejected_before_creating_output(tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail("an invalid validation mode must not connect to Docker")

    monkeypatch.setattr(session, "connect_docker", forbidden)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="validation_mode must be 'strict' or 'lenient'"):
        run_replay(tmp_path / "source", "C0", output=output, validation_mode="unknown")
    assert not output.exists()


@pytest.mark.parametrize("difference_kind", ["tool", "file"])
def test_strict_final_complete_step_is_completed_with_difference(
    tmp_path, replay_session, difference_kind,
):
    state = replay_session
    state.different_call = 2 if difference_kind == "tool" else None
    state.file_difference = difference_kind == "file"
    output = tmp_path / "output"
    report = run_replay(state.source, "C0", "C1", output=output, validation_mode="strict")
    assert report["validation_mode"] == "strict"
    assert report["execution_status"] == "completed"
    assert report["validation_status"] == report["status"] == "difference"
    assert report["first_difference"]["source_index"] == 1
    assert not report["incomplete_step"] and "stop_reason" not in report
    assert len(state.sandbox.calls) == 2 and len(report["mappings"]) == 2
    assert report["cleanup_confirmed"]
    assert json.loads((output / "report.json").read_text()) == report


@pytest.mark.parametrize("fault,expected_calls,expected_nodes", [
    ("snapshot-read", 0, 0), ("baseline", 0, 0), ("context", 0, 0),
    ("tool", 2, 1), ("unknown", 2, 1),
    ("compare", 2, 2), ("snapshot-capture", 3, 2),
])
@pytest.mark.parametrize("validation_mode,prior_difference", [
    ("strict", False), ("lenient", False), ("lenient", True),
])
def test_validation_modes_stop_faults_and_preserve_boundaries(
    tmp_path, replay_session, fault, expected_calls, expected_nodes, validation_mode, prior_difference,
):
    state = replay_session
    state.fault = fault
    state.different_call = 1 if prior_difference else None
    output = tmp_path / "output"
    events = []
    report = run_replay(state.source, "C0", "C3", output=output, validation_mode=validation_mode,
                        progress=events.append)
    observed_difference = prior_difference and expected_calls > 0
    assert report["status"] == report["execution_status"] == "failed"
    assert report["validation_status"] == ("difference" if observed_difference else "incomplete")
    assert ("first_difference" in report) == observed_difference
    assert len(state.sandbox.calls) == expected_calls
    assert "stop_reason" not in report
    assert report["cleanup_confirmed"] and state.closed == ["restore", "client"]
    if expected_nodes:
        with RecordStore.open(output / "record") as store:
            assert len(store.list(Node)) == expected_nodes
        assert len(report.get("mappings", [])) == (expected_nodes if fault != "baseline" else 0)
    else:
        assert not (output / "record").exists()
    if observed_difference:
        assert report["first_difference"] == report["steps"][0]["tools"][0]
    if fault == "unknown":
        assert report["steps"][0]["tools"][1]["status"] == "unknown"
        assert report["incomplete_step"]
    if fault == "snapshot-capture":
        assert report["incomplete_step"] and "node_id" not in report["steps"][1]
    assert json.loads((output / "report.json").read_text()) == report
    assert events[-1].status == "failed"
    assert events[-2].phase == "cleanup"
    assert max((event.step for event in events if event.phase == "committed"), default=0) < 3


def test_comparison_fault_keeps_prior_file_difference(tmp_path, replay_session):
    state = replay_session
    state.file_difference = True
    state.fault = "compare-after-file"
    report = run_replay(state.source, "C0", "C3", output=tmp_path / "output",
                        validation_mode="lenient")
    assert report["execution_status"] == "failed" and report["validation_status"] == "difference"
    assert len(state.sandbox.calls) == 3 and len(report["mappings"]) == 3
    assert report["first_difference"] == report["steps"][0]["files"]
    assert report["steps"][1]["starting_divergent"]
    assert not report["incomplete_step"]
    assert report["cleanup_confirmed"]


@pytest.mark.parametrize("validation_mode", ["strict", "lenient"])
@pytest.mark.parametrize("cleanup_fault", [False, True])
def test_restore_failure_retains_attached_cleanup_evidence(
    tmp_path, replay_session, validation_mode, cleanup_fault,
):
    state = replay_session
    state.fault, state.cleanup_fault = "restore", cleanup_fault
    report = run_replay(state.source, "C0", "C3", output=tmp_path / "output",
                        validation_mode=validation_mode)
    assert report["execution_status"] == "failed" and report["validation_status"] == "incomplete"
    assert report["error"] == "restore fault" and not state.sandbox.calls
    assert report["cleanup_confirmed"] is (not cleanup_fault)
    assert report["restore_report"]["cleanup_confirmed"] is (not cleanup_fault)
    assert report["cleanup_errors"] == (["restore: pool cleanup fault"] if cleanup_fault else [])
    assert state.closed == ["restore-internal", "client"]


@pytest.mark.parametrize("difference", [False, True])
@pytest.mark.parametrize("validation_mode", ["strict", "lenient"])
def test_cleanup_failure_overrides_execution_without_erasing_validation(
    tmp_path, replay_session, difference, validation_mode,
):
    state = replay_session
    state.cleanup_fault = True
    state.file_difference = difference
    report = run_replay(state.source, "C0", "C3", output=tmp_path / "output",
                        validation_mode=validation_mode)
    assert report["execution_status"] == report["status"] == "failed"
    assert report["validation_status"] == ("difference" if difference else "match")
    assert ("first_difference" in report) == difference
    assert not report["cleanup_confirmed"] and "restore cleanup fault" in report["cleanup_errors"][0]
    assert "stop_reason" not in report
    assert state.closed == ["restore", "client"]


def test_termination_fault_preserves_primary_error_and_cleanup(tmp_path, replay_session):
    state = replay_session
    state.fault, state.different_call, state.termination_fault = "tool", 1, True
    report = run_replay(state.source, "C0", "C3", output=tmp_path / "output",
                        validation_mode="lenient")
    assert report["error"] == "unknown execution fault"
    assert report["termination_error"] == "RuntimeError: termination fault"
    assert report["execution_status"] == "failed" and report["validation_status"] == "difference"
    assert report["cleanup_confirmed"] and state.closed == ["restore", "client"]


@pytest.mark.parametrize("validation_mode", ["strict", "lenient"])
def test_corrupt_material_stops_before_restore(tmp_path, replay_session, validation_mode):
    state = replay_session
    with RecordStore.open(state.source) as store:
        path = store._artifacts._path(state.config)
    path.chmod(0o600)
    path.write_bytes(b"corrupt")
    report = run_replay(state.source, "C0", "C3", output=tmp_path / "output",
                        validation_mode=validation_mode)
    assert report["execution_status"] == "failed" and report["validation_status"] == "incomplete"
    assert report["phase"] == "preflight" and not state.restored
    assert not state.sandbox.calls and not state.closed
    assert report["cleanup_confirmed"]
