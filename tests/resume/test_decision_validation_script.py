import pytest

from examples import resume_decision_validation as validation
from palinreplay.core.errors import RecordUsageError
from palinreplay.record.recorder import Recorder
from palinreplay.replay.environment import RestoredExecutor
from palinreplay.resume import decide_resume_start
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.store import RecordStore
from tests.resume import test_decision

session = test_decision.session
llm_config = test_decision.llm_config


class Container:
    id = "acceptance-container"

    def exec_run(self, *args, **kwargs):
        raise AssertionError("unexpected inspection")


@pytest.fixture
def boundary_session(session):
    session.restored.container = Container()
    return session


def counts():
    return dict.fromkeys((*validation.ZERO_COUNTS, "model_requests", "http_requests"), 0)


@pytest.mark.parametrize("instruction", [None, validation.INSTRUCTION])
def test_guard_permits_one_actual_decision_and_observes_pending_actions(
    boundary_session, llm_config, instruction,
):
    observed = counts()
    response = validation.controlled_response("/testbed/unique-pending-marker")
    with validation.decision_boundary(boundary_session, observed, controlled=response):
        decision = decide_resume_start(boundary_session, instruction=instruction, llm_config=llm_config)
    assert observed["model_requests"] == 1 and observed["http_requests"] == 0
    assert all(observed[name] == 0 for name in validation.ZERO_COUNTS)
    assert len(decision.messages) == 42 + (instruction is not None)
    assert len(decision.actions) == 2
    assert "/testbed/unique-pending-marker" in decision.actions[0]["command"]
    assert not boundary_session.executor.mock_calls


@pytest.mark.parametrize("operation,counter", [
    (lambda: RestoredExecutor.exec(None, "touch marker"), "tool_executions"),
    (lambda: RestoredExecutor.execute(None, None), "tool_executions"),
    (lambda: Container().exec_run("touch marker"), "tool_executions"),
    (lambda: BtrfsPool.command(None, "snapshot"), "file_snapshots"),
    (lambda: RecordStore.commit_node(None), "complete_nodes"),
    (lambda: Recorder.begin_step(None), "recording_operations"),
])
def test_guard_records_and_rejects_environment_changes(boundary_session, operation, counter):
    observed = counts()
    with validation.decision_boundary(boundary_session, observed):
        with pytest.raises(AssertionError, match=counter):
            operation()
    assert observed[counter] == 1
    assert observed["model_requests"] == 0


def test_semantic_comparison_preserves_contents_and_tool_relations():
    first = [{"role": "assistant", "content": None, "extra": {"actions": []}, "tool_calls": [{
        "id": "tool-1", "type": "function", "function": {"name": "bash", "arguments": "{}"},
    }]}, {"role": "tool", "tool_call_id": "tool-1", "content": "result"}]
    wire = [{key: value for key, value in message.items() if key != "extra" and value is not None}
            for message in first]
    assert validation.semantic_messages(first) == validation.semantic_messages(wire)
    wire[-1] = wire[-1] | {"tool_call_id": "wrong-id"}
    assert validation.semantic_messages(first) != validation.semantic_messages(wire)


def test_runner_output_cannot_overwrite_source_recording(tmp_path):
    source = tmp_path / "recording" / "record"
    source.mkdir(parents=True)
    output = source.parent / "acceptance"
    with pytest.raises(RecordUsageError, match="outside"):
        validation.main(source, tmp_path / "services.json", output,
                        model="openai/M", llm_config=tmp_path / "llm.toml")
    assert not output.exists()
