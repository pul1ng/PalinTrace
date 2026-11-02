import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

import palinreplay.replay.session as session
from palinreplay.agents import AgentTask
from palinreplay.core.models import Node, State
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
                            comparisons=[], captures=0, restored=False, recorder=None)
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
            if op == "verify" and state.fault == "baseline":
                raise RuntimeError("baseline verification fault")
            assert op in ("verify", "usage")
            return {"equal": True}

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
                               report={"snapshot": Snapshot(location=location, name="bases/C0",
                                                            uuid=uuid4()).model_dump(mode="json")})
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

    executor.execute = execute
    client = SimpleNamespace(close=lambda: state.closed.append("client"))
    state.client = client

    def restore(*args, **kwargs):
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


@pytest.mark.parametrize("validation_mode", [None, "strict", "lenient"])
def test_session_mode_reaches_report_recording_and_loop(tmp_path, replay_session, validation_mode):
    state = replay_session
    state.different_call = 1
    options = {} if validation_mode is None else {"validation_mode": validation_mode}
    output = tmp_path / "output"
    report = run_replay(state.source, "C0", "C3", output=output, **options)
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
    ("snapshot-read", 0, 0), ("baseline", 0, 1), ("tool", 2, 1), ("unknown", 2, 1),
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
    report = run_replay(state.source, "C0", "C3", output=output, validation_mode=validation_mode)
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
