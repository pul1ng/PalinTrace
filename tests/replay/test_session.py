import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

import palinreplay.replay.session as session
from palinreplay.agents import AgentTask
from palinreplay.core.errors import IntegrityError
from palinreplay.core.models import Failure, Node, Run, RunEnd, State, StateCapture, Step
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
    assert report["execution_status"] == "failed" and report["phase"] == "preflight"
    assert report["schema_version"] == "palinreplay.replay-report.v2"
    assert "validation_status" not in report and "validation_mode" not in report
    assert "version" in report["error"]
    assert report["cleanup_confirmed"]
    assert json.loads((output / "report.json").read_text()) == report


@pytest.fixture
def replay_session(tmp_path, monkeypatch):
    """Real history, session and recorder, with only the external Btrfs boundary replaced."""
    state = SimpleNamespace(fault=None, different_call=None, file_difference=False,
                            difference_kind="output", mtime_only=False,
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
            if state.fault == "snapshot-capture" and state.captures == 2:
                raise RuntimeError("snapshot capture fault")
            return {"uuid": str(uuid4())}

        def command(self, op, **kwargs):
            if op == "verify":
                assert state.captures == 0, "full R1 check must precede initial capture"
            if op == "verify" and state.fault == "baseline":
                raise RuntimeError("baseline verification fault")
            assert op in ("verify", "usage")
            return {"equal": True}

        def changes(self, *args):
            pytest.fail("preparation file comparison must not run")

        def _file_command(self, *args, **kwargs):
            pytest.fail("preparation file comparison must not run")

        def compare_incremental(self, *args):
            pytest.fail("runtime file comparison must not run")

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
        fault_call = 3 if state.mtime_only else 2
        if state.fault == "tool" and len(sandbox.calls) == fault_call:
            raise SandboxTimeout("unknown execution fault")
        if state.fault == "unknown" and len(sandbox.calls) == fault_call:
            return ExecResult(result.command, result.output, None, 1)
        if len(sandbox.calls) == state.different_call:
            return ExecResult(result.command,
                              b"actual" if state.difference_kind == "output" else result.output,
                              1 if state.difference_kind == "exit-code" else result.exit_code, 1)
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


@pytest.mark.parametrize("prepared", [False, True])
def test_empty_range_restores_and_prepares_without_calls_or_snapshots(tmp_path, replay_session, prepared):
    state = replay_session
    services = None
    commands = []
    if prepared:
        services = tmp_path / "services.json"
        services.write_text(json.dumps({"services": [{
            "name": "A", "start_command": "start A", "readiness_command": "ready A", "timeout_sec": 10,
        }]}))
        def execute(command, **kwargs):
            assert state.executor.restored.report["checks"]["full_filesystem"]
            commands.append(command)
            return ExecResult(command, b"ready", 0, 0.1)
        state.executor.exec = execute
    output = tmp_path / "output"
    report = run_replay(state.source, "C2", "C2", output=output, services_config=services)
    assert report["execution_status"] == "completed" and report["r1"]["status"] == "passed"
    assert not state.sandbox.calls and state.captures == 0
    assert commands == (["start A", "ready A", "ready A"] if prepared else [])
    assert report["mappings"] == report["steps"] == [] and not report["incomplete_step"]
    assert report["statistics"]["actual_tool_executions"] == 0
    with RecordStore.open(output / "record") as store:
        assert store.list(Node) == store.list(State) == store.list(StateCapture) == store.list(Step) == ()
        end, = store.list(RunEnd)
        assert end.status == "completed" and end.last_node_id is None
        assert store.replay_source(end.run_id).start.index == 2
        assert store.check_integrity().ok
    assert report["cleanup_confirmed"]


@pytest.mark.parametrize("difference", [False, True])
def test_services_prepare_after_r1_before_reference_initialization_and_leave_history_unchanged(tmp_path, replay_session, difference):
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
    assert "validation_status" not in report and "status" not in report
    assert report["actual_node_count"] == report["completed_steps"] == 3
    assert report["source_start"]["index"] == 0 and report["source_start"]["reference"]
    assert not report["preparation_state_captured"]
    assert state.captures == 3
    assert json.loads((output / "preparation.json").read_text()) == report["preparation"]
    assert not (output / "preparation-files.json").exists()
    assert "preparation_files" not in report
    assert [event.phase for event in events].index("service-preparation") < [event.phase for event in events].index("committed")
    assert [event.step for event in events if event.phase == "committed"] == [1, 2, 3]
    assert report["statistics"]["actual_model_calls"] == 0
    assert report["statistics"]["actual_tool_executions"] == 4
    with RecordStore.open(state.source) as origin, RecordStore.open(output / "record") as actual:
        history = load_replay_materials(origin, pin_replay(origin, "C0", "C3"))
        for step, mapping in zip(history.steps, report["mappings"], strict=True):
            node = actual.get(Node, mapping["node_id"])
            for tool, execution_id in zip(step.tools, node.tool_execution_ids, strict=True):
                trace = read_call(actual, execution_id)
                assert actual.read_json(trace.execution.request) == tool.request
        saved = actual.read_json(actual.get(Run, report["run_id"]).config)["replay"]
        assert saved["source_start"]["context"] == history.context
        assert not saved["preparation_state_captured"]
        assert [n.index for n in actual.list(Node)] == [1, 2, 3]
    assert state.executor.runtime.launch.workdir == "/testbed"
    assert state.executor.runtime.launch.environment == {"PATH": "/bin"}
    assert report["cleanup_confirmed"]


@pytest.mark.parametrize("damage", ["missing-artifact", "missing-config", "missing-evidence", "changed-output"])
def test_preparation_material_is_inline_and_covered_by_record_integrity(tmp_path, replay_session, damage):
    state = replay_session
    config = tmp_path / "services.json"
    config.write_text(json.dumps({"services": [
        {"name": "A", "start_command": "start A", "readiness_command": "ready A", "timeout_sec": 10},
    ]}))
    state.executor.exec = lambda command, **kwargs: ExecResult(command, b"\xff\x00ready\n", 0, 0.1)
    output = tmp_path / "output"
    report = run_replay(state.source, "C0", "C3", output=output, services_config=config)
    assert report["execution_status"] == "completed" and report["preparation"]["status"] == "ready"
    # The recorded materials must survive without the original input or external journal.
    config.unlink()
    (output / "preparation.json").unlink()
    with RecordStore.open(output / "record") as store:
        run = store.get(Run, report["run_id"])
        recorded = store.read_json(run.config)
        preparation = recorded["replay"]["preparation"]
        assert preparation["config"] == report["preparation"]["config"]
        assert preparation["evidence"] == report["preparation"]
        assert store.check_integrity().ok
        path = store._artifacts._path(run.config)
    if damage == "missing-artifact":
        path.unlink()
    else:
        if damage == "changed-output":
            preparation["evidence"]["services"][0]["startup"]["output_base64"] = "dGFtcGVyZWQ="
        else:
            del preparation[damage.removeprefix("missing-")]
        path.chmod(0o600)
        path.write_text(json.dumps(recorded))
    with RecordStore.open(output / "record") as store:
        audit = store.check_integrity()
        assert not audit.ok
        assert any(issue.record == "run/" + run.run_id and run.config.sha256 in issue.message
                   for issue in audit.issues)
        with pytest.raises(IntegrityError, match=run.config.sha256):
            store.read_json(run.config)


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
    assert report["execution_status"] == ("cancelled" if fault == "cancel" else "failed")
    assert report["phase"] == "service-preparation"
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
    assert report["execution_status"] == (
        "cancelled" if fault.startswith("cancel") and not cleanup_fault else "failed"
    )
    assert report["phase"] == "service-preparation"
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


@pytest.mark.parametrize("different_call", [1, 2, 3])
@pytest.mark.parametrize("kind", ["output", "exit-code"])
def test_session_preserves_actual_results_and_stops_only_on_exit_codes(
    tmp_path, replay_session, different_call, kind,
):
    state = replay_session
    state.different_call, state.difference_kind = different_call, kind
    output = tmp_path / "output"
    report = run_replay(state.source, "C0", "C3", output=output)
    stopped = kind == "exit-code"
    assert report["execution_status"] == ("stopped" if stopped else "completed")
    assert len(state.sandbox.calls) == (different_call if stopped else 4)
    with RecordStore.open(output / "record") as store:
        config = store.read_json(state.recorder.run.config)["replay"]
        assert config["execution_policy"] == "historical-exit-code-v1"
        assert "validation_mode" not in config
        assert len(store.list(Node)) == (different_call - 1 if stopped else 3)
        assert report["actual_node_count"] == report["completed_steps"] == len(store.list(Node))
        assert report["incomplete_step"] is (stopped and different_call == 1)
        if stopped:
            mismatch = report["exit_mismatch"]
            assert mismatch["actual_index"] == mismatch["source_index"]
            assert mismatch["tool_ordinal"] == mismatch["tool_index"] + 1
            assert report["stop_reason"] == "exit-code"
            trace = read_call(store, mismatch["execution_id"])
            assert trace.execution.request.model_dump(mode="json") == mismatch["request_ref"]
            assert trace.events[1].payload.model_dump(mode="json") == mismatch["result_ref"]
            assert trace.events[1].details.model_dump(mode="json") == mismatch["result_details_ref"]
            assert ("node_id" in mismatch) is (different_call != 1)
            assert bool(report["incomplete_steps"]) is (different_call == 1)
            assert store.read_json(trace.events[1].details)["backend"]["decoded"]["returncode"] == 1
        else:
            tools = [tool for step in report["steps"] for tool in step["tools"]]
            trace = read_call(store, tools[different_call - 1]["execution_id"])
            assert store.read_bytes(trace.events[1].payload) == b"actual"
        assert store.check_integrity().ok
    assert report["cleanup_confirmed"] and state.closed == ["restore", "client"]
    assert state.recorder.phase == "closed"
    assert json.loads((output / "report.json").read_text()) == report


def test_session_endpoint_exit_mismatch_remains_stopped(tmp_path, replay_session):
    state = replay_session
    state.different_call, state.difference_kind = 2, "exit-code"
    report = run_replay(state.source, "C0", "C1", output=tmp_path / "output")
    assert report["execution_status"] == "stopped" and report["stop_reason"] == "exit-code"
    assert not report["incomplete_step"] and len(report["mappings"]) == 1
    assert report["mappings"][0]["index"] == 1 and state.captures == 1


def test_progress_reports_only_actual_commits(tmp_path, replay_session):
    events = []
    report = run_replay(replay_session.source, "C1", "C3", output=tmp_path / "output",
                        progress=events.append)
    assert report["execution_status"] == "completed"
    assert [(e.source_step, e.step) for e in events if e.phase == "committed"] == [(2, 2), (3, 3)]
    assert all(e.total_steps == 2 for e in events if e.total_steps is not None)


def test_progress_observer_error_does_not_change_execution(tmp_path, replay_session):
    observed = []
    def broken(event):
        observed.append(event)
        raise OSError("terminal disconnected")
    report = run_replay(replay_session.source, "C0", "C3", output=tmp_path / "output", progress=broken)
    assert len(observed) == 1 and report["execution_status"] == "completed"
    assert len(replay_session.sandbox.calls) == 4 and report["cleanup_confirmed"]


@pytest.mark.parametrize("fault,expected_calls,expected_nodes", [
    ("snapshot-read", 0, 0), ("baseline", 0, 0), ("context", 0, 0),
    ("tool", 2, 0), ("unknown", 2, 0), ("snapshot-capture", 3, 1),
])
def test_session_faults_keep_only_actual_complete_nodes(
    tmp_path, replay_session, fault, expected_calls, expected_nodes,
):
    state = replay_session
    state.fault = fault
    output = tmp_path / "output"
    report = run_replay(state.source, "C0", "C3", output=output)
    assert report["execution_status"] == "failed"
    assert len(state.sandbox.calls) == expected_calls
    assert report["cleanup_confirmed"] and state.closed == ["restore", "client"]
    if expected_calls:
        with RecordStore.open(output / "record") as store:
            assert len(store.list(Node)) == expected_nodes
            assert store.check_integrity().ok
        assert report["incomplete_step"]
    else:
        assert not (output / "record").exists()
    assert json.loads((output / "report.json").read_text()) == report


@pytest.mark.parametrize("cleanup_fault", [False, True])
def test_restore_failure_retains_cleanup_evidence(tmp_path, replay_session, cleanup_fault):
    state = replay_session
    state.fault, state.cleanup_fault = "restore", cleanup_fault
    report = run_replay(state.source, "C0", "C3", output=tmp_path / "output")
    assert report["execution_status"] == "failed" and report["error"] == "restore fault"
    assert not state.sandbox.calls
    assert report["cleanup_confirmed"] is (not cleanup_fault)
    assert report["restore_report"]["cleanup_confirmed"] is (not cleanup_fault)
    assert state.closed == ["restore-internal", "client"]


def test_cleanup_failure_prevents_success(tmp_path, replay_session):
    state = replay_session
    state.cleanup_fault = True
    report = run_replay(state.source, "C0", "C3", output=tmp_path / "output")
    assert report["execution_status"] == "failed" and not report["cleanup_confirmed"]
    assert "restore cleanup fault" in report["cleanup_errors"][0]
    assert state.closed == ["restore", "client"]


def test_termination_fault_keeps_primary_error(tmp_path, replay_session):
    state = replay_session
    state.fault, state.termination_fault = "tool", True
    report = run_replay(state.source, "C0", "C3", output=tmp_path / "output")
    assert report["error"] == "unknown execution fault"
    assert report["termination_error"] == "RuntimeError: termination fault"
    assert report["cleanup_confirmed"]


@pytest.mark.parametrize("different_call", [1, 2])
def test_cleanup_failure_retains_mismatch_and_durable_end(tmp_path, replay_session, different_call):
    state = replay_session
    state.cleanup_fault = True
    state.different_call, state.difference_kind = different_call, "exit-code"
    output = tmp_path / "output"
    report = run_replay(state.source, "C0", "C3", output=output)
    assert report["execution_status"] == "failed" and report["stop_reason"] == "exit-code"
    assert report["exit_mismatch"]["actual_exit_code"] == 1
    assert len(state.sandbox.calls) == different_call and state.closed == ["restore", "client"]
    with RecordStore.open(output / "record") as store:
        end, = store.list(RunEnd)
        assert end.status == ("incomplete" if different_call == 1 else "completed")
        failures = [f for f in store.list(Failure) if f.category == "cleanup"]
        assert len(failures) == 1
        assert store.read_json(failures[0].details)["resource"] == "restore"
        assert len(store.list(Node)) == different_call - 1
        assert store.check_integrity().ok


@pytest.mark.parametrize("fault,calls,nodes", [
    ("adoption", 2, 0), ("first-capture", 2, 0), ("node-persistence", 2, 0),
    ("cancel-first", 1, 0), ("cancel-later", 3, 1),
])
def test_failures_and_cancellation_keep_real_tail_without_reexecution(
    tmp_path, replay_session, monkeypatch, fault, calls, nodes,
):
    state = replay_session
    output = tmp_path / "output"
    def fail(*args, **kwargs):
        raise RuntimeError(fault)
    if fault == "adoption":
        state.different_call, state.difference_kind = 2, "exit-code"
        monkeypatch.setattr(session.ReplayContext, "accept_results", fail)
    elif fault == "first-capture":
        state.executor.pool.snapshot = fail
    elif fault == "node-persistence":
        monkeypatch.setattr(RecordStore, "commit_node", fail)
    else:
        execute = state.executor.execute
        def cancelled(request):
            result = execute(request)
            if len(state.sandbox.calls) == calls:
                raise KeyboardInterrupt(fault)
            return result
        state.executor.execute = cancelled
    if fault.startswith("cancel"):
        with pytest.raises(KeyboardInterrupt, match=fault):
            run_replay(state.source, "C0", "C3", output=output)
        report = json.loads((output / "report.json").read_text())
    else:
        report = run_replay(state.source, "C0", "C3", output=output)
    assert report["execution_status"] == ("cancelled" if fault.startswith("cancel") else "failed")
    assert report["incomplete_step"]
    assert report["cleanup_confirmed"] and len(state.sandbox.calls) == calls
    if fault == "adoption":
        assert report["stop_reason"] == "exit-code" and report["exit_mismatch"]
    with RecordStore.open(output / "record") as store:
        assert len(store.list(Node)) == nodes
        end, = store.list(RunEnd)
        assert end.status == ("cancelled" if fault.startswith("cancel") else "failed")
        assert (end.last_node_id is not None) == bool(nodes)
        assert store.check_integrity().ok


@pytest.mark.parametrize("during", ["r1", "capture"])
def test_unpause_error_does_not_mask_primary_failure(tmp_path, replay_session, during):
    state = replay_session
    if during == "r1":
        state.fault = "baseline"
    else:
        def fail_capture(*args):
            state.captures += 1
            raise RuntimeError("primary capture fault")
        state.executor.pool.snapshot = fail_capture
    def unpause():
        if during == "r1" or state.captures:
            raise RuntimeError("unpause fault")
    state.executor._container.unpause = unpause
    report = run_replay(state.source, "C0", "C3", output=tmp_path / "output")
    if during == "r1":
        assert "baseline verification fault" in report["error"]
    else:
        assert "primary capture fault" in report["cause"]["error"]
    assert "unpause fault" in str(report["error_notes"])
    assert report["cleanup_confirmed"]
    if during == "capture":
        assert not report["mappings"]


@pytest.mark.parametrize("cancel", [False, True])
def test_report_failure_does_not_mask_primary_or_skip_cleanup(tmp_path, replay_session, monkeypatch, cancel):
    state = replay_session
    write = session.write_json
    def broken(path, document, **kwargs):
        if path.name == "report.json" and kwargs.get("replace"):
            raise OSError("report disk fault")
        write(path, document, **kwargs)
    monkeypatch.setattr(session, "write_json", broken)
    if cancel:
        def interrupted(*args):
            raise KeyboardInterrupt("primary cancellation")
        state.executor.execute = interrupted
        with pytest.raises(KeyboardInterrupt, match="primary cancellation"):
            run_replay(state.source, "C0", "C3", output=tmp_path / "output")
    else:
        report = run_replay(state.source, "C0", "C3", output=tmp_path / "output")
        assert report["execution_status"] == "failed"
        assert "report disk fault" in report["report_errors"][0]
    assert state.closed == ["restore", "client"] and state.recorder.phase == "closed"


def test_corrupt_material_stops_before_restore(tmp_path, replay_session):
    state = replay_session
    with RecordStore.open(state.source) as store:
        path = store._artifacts._path(state.config)
    path.chmod(0o600)
    path.write_bytes(b"corrupt")
    report = run_replay(state.source, "C0", "C3", output=tmp_path / "output")
    assert report["execution_status"] == "failed" and report["phase"] == "preflight"
    assert not state.restored and not state.sandbox.calls and not state.closed
