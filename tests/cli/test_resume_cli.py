import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from palinreplay.cli import main
from palinreplay.cli.app import build_parser
from palinreplay.storage.store import RecordStore
from tests.resume import test_decision as decision_tests
from tests.resume import test_services as service_tests
from tests.resume import test_session as session_tests
from tests.resume.test_start import history

restoration = session_tests.setup
setup = session_tests.setup
prepared = service_tests.prepared
decision_session = decision_tests.session
model_response = decision_tests.response


def test_resume_help(capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["--help"])
    assert stopped.value.code == 0
    assert "resume" in capsys.readouterr().out
    with pytest.raises(SystemExit) as stopped:
        main(["resume", "--help"])
    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    for option in ("SOURCE", "--from", "--run", "--max-steps", "--llm-config",
                   "--instruction", "--services-config", "--output", "--progress"):
        assert option in help_text
    assert "default: 20" in help_text
    assert "including the first request" in help_text
    assert "submit early" in help_text
    assert "--to" not in help_text


@pytest.mark.parametrize("options", [
    [], ["source"], ["--from", "C20"],
    ["source", "--from", "C20", "--max-steps", "0"],
    ["source", "--from", "C20", "--max-steps", "-1"],
    ["source", "--from", "C20", "--max-steps", "1.5"],
    ["source", "--from", "C20", "--instruction", ""],
    ["source", "--from", "C20", "--instruction", " \n\t"],
])
def test_invalid_arguments_stop_before_execution(monkeypatch, capsys, options):
    import palinreplay.cli.resume as cli

    def forbidden(*args, **kwargs):
        pytest.fail("invalid input must not reach Docker or model execution")

    monkeypatch.setattr(cli, "execute", forbidden)
    with pytest.raises(SystemExit) as stopped:
        main(["resume", *options])
    assert stopped.value.code == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("budget", [1, 2, 100])
def test_all_parameters_are_parsed_and_dispatched_unchanged(monkeypatch, budget):
    import palinreplay.cli.resume as cli

    seen = []
    monkeypatch.setattr(cli, "execute", lambda args: seen.append(args) or 7)
    instruction = "  下一步先检查 B 的日志\n"
    assert main([
        "resume", "source/record", "--from", "node-id", "--run", "run-id",
        "--max-steps", str(budget), "--llm-config", "model.toml",
        "--instruction", instruction, "--services-config", "services.json", "--output", "branch",
    ]) == 7
    args = seen[0]
    assert (args.source, args.start, args.run_id) == (Path("source/record"), "node-id", "run-id")
    assert args.max_steps == budget
    assert args.llm_config == Path("model.toml")
    assert args.instruction == instruction
    assert args.services_config == Path("services.json")
    assert args.output == Path("branch")


def test_resume_defaults():
    args = build_parser().parse_args(["resume", "source", "--from", "C20"])
    assert args.max_steps == 20
    assert args.run_id is args.llm_config is args.instruction is args.services_config is None
    assert args.output is None
    assert args.progress == "auto"


@pytest.fixture
def private_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("PALINREPLAY_LLM_CONFIG", "OPENAI_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "private.toml"
    path.write_text('[llm]\nmodel = "branch-model"\napi_key = "test-resume-secret"\n')
    return path


@pytest.mark.parametrize("mode", ["explicit", "default-file", "environment-path", "environment-only"])
def test_model_loading_reuses_configuration_rules_without_requests(
    tmp_path, monkeypatch, capsys, private_config, mode,
):
    import palinreplay.cli.resume as cli
    import palinreplay.llm as llm

    def forbidden(*args, **kwargs):
        pytest.fail("configuration loading must not create a model client")

    monkeypatch.setattr(llm, "OpenAI", forbidden)
    path = None
    if mode == "explicit":
        path = private_config
    elif mode == "default-file":
        default = tmp_path / "config/llm/llm.toml"
        default.parent.mkdir(parents=True)
        default.write_bytes(private_config.read_bytes())
    elif mode == "environment-path":
        monkeypatch.setenv("PALINREPLAY_LLM_CONFIG", str(private_config))
    else:
        monkeypatch.setenv("OPENAI_MODEL", "branch-model")
        monkeypatch.setenv("OPENAI_API_KEY", "test-resume-secret")
    assert cli._load_model(path) == "branch-model"
    output = capsys.readouterr()
    assert "test-resume-secret" not in output.out + output.err
    assert not (tmp_path / "outputs").exists()


@pytest.mark.parametrize("document", [
    None,
    '[llm]\napi_key = "test-resume-secret"\n',
    '[llm]\nmodel = "branch-model"\n',
    '[llm]\nmodel = "branch-model"\napi_key = "test-resume-secret"\n'
    'base_url = "https://test-resume-secret@example.invalid"\n',
    'api_key = "test-resume-secret"\n',
    '[llm]\nmodel = "branch-model"\napi_key = "test-resume-secret"\nbad = [\n',
])
def test_configuration_error_is_early_and_does_not_print_secrets(
    tmp_path, monkeypatch, capsys, private_config, document,
):
    import palinreplay.sandbox.client as sandbox

    def forbidden(*args, **kwargs):
        pytest.fail("configuration errors must precede Docker")

    monkeypatch.setattr(sandbox, "connect_docker", forbidden)
    if document is None:
        private_config.unlink()
    else:
        private_config.write_text(document)
    assert main(["resume", "source", "--from", "C20", "--llm-config", str(private_config)]) == 1
    output = capsys.readouterr()
    assert "error:" in output.err
    assert "test-resume-secret" not in output.out + output.err
    assert not (tmp_path / "outputs").exists()


@pytest.fixture
def source_record(tmp_path):
    path = tmp_path / "source" / "record"
    with RecordStore.create(path) as store:
        recorder, _ = history(store)
        node = recorder.current_node
        recorder.finish()
    return path, node


@pytest.mark.parametrize("use_store", [False, True])
@pytest.mark.parametrize("full_id", [False, True])
def test_cli_selection_reuses_source_resolution(
    monkeypatch, private_config, source_record, use_store, full_id,
):
    import palinreplay.cli.resume as cli
    import palinreplay.sandbox.client as sandbox
    from palinreplay.llm import OpenAIChatClient

    def forbidden(*args, **kwargs):
        pytest.fail("selection must not restore or request a model")

    monkeypatch.setattr(sandbox, "connect_docker", forbidden)
    monkeypatch.setattr(OpenAIChatClient, "complete", forbidden)
    path, node = source_record
    args = build_parser().parse_args([
        "resume", str(path if use_store else path.parent),
        "--from", node.node_id if full_id else "C20", "--max-steps", "2",
    ])
    selected = cli._select_start(args, cli._load_model(private_config))
    assert selected.source_store == path
    assert selected.node == node
    assert selected.model == "branch-model"
    assert selected.max_steps == 2


def test_source_ambiguity_requires_run_and_preserves_selected_node(private_config, source_record):
    import palinreplay.cli.resume as cli
    from palinreplay.core.errors import RecordUsageError

    path, first = source_record
    with RecordStore.open(path, read_only=False) as store:
        recorder, _ = history(store)
        second = recorder.current_node
        recorder.finish()
    args = build_parser().parse_args(["resume", str(path), "--from", "C20"])
    with pytest.raises(RecordUsageError, match="use --run"):
        cli._select_start(args, "branch-model")
    for node in (first, second):
        args.run_id = node.run_id
        assert cli._select_start(args, "branch-model").node == node


@pytest.mark.parametrize("selector,run_id", [("C21", None), ("missing", None), ("C20", "missing")])
def test_cli_source_errors_are_actionable(private_config, source_record, capsys, selector, run_id):
    path, _ = source_record
    options = [] if run_id is None else ["--run", run_id]
    assert main(["resume", str(path), "--from", selector,
                 "--llm-config", str(private_config), *options]) == 1
    output = capsys.readouterr()
    assert "no committed node" in output.err
    assert "test-resume-secret" not in output.out + output.err


def test_default_output_uses_the_selected_run_task_without_creating_directory(tmp_path, monkeypatch):
    import palinreplay.cli.resume as cli
    from palinreplay.core.interfaces import BoundaryAdapter, Payload
    from palinreplay.record.recorder import Recorder
    from tests.helpers.recorder_smoke import FakeAgent, FakeStateBackend

    path = tmp_path / "many-runs" / "record"
    with RecordStore.create(path) as store:
        for task in ("first-task", "selected task"):
            agent, backend = FakeAgent(), FakeStateBackend()
            with Recorder(store, task_id=task, agent_name="fixture", agent_version="1",
                          config=Payload.json({}),
                          boundary=BoundaryAdapter(agent.export, backend.capture)) as recorder:
                node = recorder.current_node
                recorder.finish()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("palinreplay.cli.outputs.timestamp", lambda: "261106_123456")
    args = build_parser().parse_args(["resume", str(path), "--from", "C0", "--run", node.run_id])
    start = cli._select_start(args, "branch-model")
    output = cli._output_path(args, start)
    assert output == tmp_path / "outputs/resume/261106_123456_selected_task"
    assert not output.parent.exists()


def test_explicit_output_is_not_created_or_renamed(source_record, monkeypatch):
    import palinreplay.cli.resume as cli

    path, _ = source_record
    args = build_parser().parse_args([
        "resume", str(path), "--from", "C20", "--output", "custom/branch",
    ])
    start = cli._select_start(args, "branch-model")

    def forbidden(*args, **kwargs):
        pytest.fail("explicit output selection must not reread source metadata")

    monkeypatch.setattr(RecordStore, "open", forbidden)
    assert cli._output_path(args, start) == Path("custom/branch")
    assert not Path("custom").exists()


def _restoration_args(restoration, private_config, *options):
    return ["resume", str(restoration.start.source_store), "--from", restoration.start.node.node_id,
            "--llm-config", str(private_config), "--output", str(restoration.output), *options]


@pytest.fixture
def restored_cli(monkeypatch):
    import palinreplay.resume as resume

    decision = Mock(return_value=SimpleNamespace(messages=[]))
    monkeypatch.setattr(resume, "decide_resume_start", decision)
    monkeypatch.setattr(resume, "run_resume_steps", Mock(return_value=[]))
    return decision


def test_cli_enters_the_existing_restored_session(restoration, private_config, restored_cli):
    assert main(_restoration_args(restoration, private_config)) == 0
    assert restored_cli.call_count == 1
    assert restored_cli.call_args.args[0].executor is restoration.executor
    restoration.validate.assert_called_once_with(restoration.materials, restoration.executor)
    restoration.restore.assert_called_once()
    restoration.executor.close.assert_called_once()
    restoration.restored.close.assert_called_once()
    restoration.connect.return_value.close.assert_called_once()
    report = json.loads((restoration.output / "report.json").read_text())
    assert report["restore_status"] == report["r1"]["status"] == "passed"
    assert report["cleanup_confirmed"]
    assert report["prefix"]["message_count"] == len(restoration.materials.context["messages"])


def test_cli_r1_failure_does_not_deliver_a_session(restoration, private_config, capsys):
    from palinreplay.core.errors import IntegrityError

    restoration.validate.side_effect = IntegrityError("R1 differs")
    assert main(_restoration_args(restoration, private_config)) == 1
    restoration.executor.close.assert_called_once()
    restoration.restored.close.assert_called_once()
    restoration.connect.return_value.close.assert_called_once()
    report = json.loads((restoration.output / "report.json").read_text())
    assert report["restore_error"]["phase"] == "validate_r1"
    assert report["caller"]["status"] == "not_started"
    assert "Restored:" not in capsys.readouterr().out


def test_cli_caller_failure_still_leaves_restoration_context(
    restoration, private_config, monkeypatch,
):
    import builtins

    original_print = builtins.print

    def fail_after_delivery(*args, **kwargs):
        if args and str(args[0]).startswith("Restored:"):
            raise RuntimeError("caller failure")
        original_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", fail_after_delivery)
    assert main(_restoration_args(restoration, private_config)) == 1
    restoration.executor.close.assert_called_once()
    restoration.restored.close.assert_called_once()
    restoration.connect.return_value.close.assert_called_once()
    report = json.loads((restoration.output / "report.json").read_text())
    assert report["caller"]["status"] == "failed" and report["cleanup_confirmed"]


@pytest.mark.parametrize("location", ["existing", "record", "recording"])
def test_cli_reuses_output_rejection_without_changing_source(
    restoration, private_config, location,
):
    if location == "existing":
        restoration.output.mkdir()
        marker = restoration.output / "report.json"
        marker.write_text("existing evidence")
    else:
        source = restoration.start.source_store
        restoration.output = (source if location == "record" else source.parent) / "new-output"
    assert main(_restoration_args(restoration, private_config)) == 1
    restoration.connect.assert_not_called()
    restoration.restore.assert_not_called()
    if location == "existing":
        assert marker.read_text() == "existing evidence"
    else:
        assert not restoration.output.exists()


def test_cli_prepares_services_once_after_r1(prepared, private_config, restored_cli):
    restoration = prepared.session
    assert main(_restoration_args(restoration, private_config,
                                  "--services-config", str(prepared.path))) == 0
    assert prepared.events == ["r1", "start A", "start B"]
    report = json.loads((restoration.output / "report.json").read_text())
    assert report["preparation"]["status"] == "ready"
    assert [item["name"] for item in report["preparation"]["services"]] == ["A", "B"]
    assert report["cleanup_confirmed"]
    restoration.restore.assert_called_once()
    restoration.executor.close.assert_called_once()


@pytest.mark.parametrize("configured", [False, True])
def test_cli_distinguishes_skipped_and_empty_preparation(
    restoration, private_config, tmp_path, restored_cli, configured,
):
    config = tmp_path / "empty-services.json"
    config.write_text('{"services": []}')
    options = ["--services-config", str(config)] if configured else []
    assert main(_restoration_args(restoration, private_config, *options)) == 0
    report = json.loads((restoration.output / "report.json").read_text())
    assert report["preparation"]["status"] == ("ready" if configured else "skipped")
    restoration.executor.exec.assert_not_called()
    assert (restoration.output / "preparation.json").exists() is configured


@pytest.mark.parametrize("fault", ["exit", "timeout"])
def test_cli_service_failure_preserves_evidence_and_never_requests_model(
    prepared, private_config, monkeypatch, fault,
):
    import palinreplay.resume as resume
    from palinreplay.sandbox.base import ExecResult, SandboxTimeout

    restoration = prepared.session

    def forbidden(*args, **kwargs):
        pytest.fail("failed preparation must not reach the model")

    monkeypatch.setattr(resume, "decide_resume_start", forbidden)

    def execute(command, **kwargs):
        if command == "start B":
            if fault == "timeout":
                raise SandboxTimeout("startup timed out")
            return ExecResult(command, b"failed", 17, 0.1)
        return ExecResult(command, b"started", 0, 0.1)

    restoration.executor.exec.side_effect = execute
    assert main(_restoration_args(restoration, private_config,
                                  "--services-config", str(prepared.path))) == 1
    report = json.loads((restoration.output / "report.json").read_text())
    assert report["r1"]["status"] == "passed"
    assert report["preparation"]["status"] == "failed"
    assert report["caller"]["status"] == "not_started"
    assert report["cleanup_confirmed"]
    restoration.executor.close.assert_called_once()
    restoration.restored.close.assert_called_once()
    restoration.connect.return_value.close.assert_called_once()


def test_cli_mismatched_manifest_fails_before_container(restoration, private_config, tmp_path):
    from tests.replay.test_service_config import _manifest

    _, manifest = _manifest(tmp_path)
    config = tmp_path / "manifest.json"
    document = manifest.model_dump(mode="json")
    document["source"]["run_id"] = "another-run"
    config.write_text(json.dumps(document))
    assert main(_restoration_args(restoration, private_config,
                                  "--services-config", str(config))) == 1
    restoration.connect.assert_not_called()
    restoration.restore.assert_not_called()
    report = json.loads((restoration.output / "report.json").read_text())
    assert report["restore_status"] == "not_started"
    assert "services_error" in report and report["cleanup_confirmed"]


@pytest.mark.parametrize("instruction", [None, "  下一步先检查 B 的日志\n"])
def test_cli_first_decision_uses_the_full_prefix_once_without_executing_tools(
    decision_session, model_response, private_config, monkeypatch, instruction,
):
    import palinreplay.cli.resume as cli
    import palinreplay.resume as resume
    from tests.resume.test_multiturn_requests import api_messages

    session = decision_session
    inherited = deepcopy(session.context)
    events = []

    def select(args, model):
        session.start.model = model
        session.start.max_steps = args.max_steps
        return session.start

    @contextmanager
    def restore(start, output, *, services_config, progress):
        assert start is session.start and output == session.output
        assert services_config == Path("services.json")
        events.append("ready")
        yield session
        events.append("cleanup")

    def complete(request, credentials):
        assert events == ["ready"]
        events.append("request")
        assert request["model"] == "openai/branch-model"
        assert request["messages"][:42] == api_messages(inherited["messages"])
        assert len(request["messages"]) == 42 + (instruction is not None)
        if instruction is not None:
            assert request["messages"][42] == {"role": "user", "content": instruction}
        return model_response

    monkeypatch.setattr(cli, "_select_start", select)
    monkeypatch.setattr(cli, "_output_path", lambda *args: session.output)
    monkeypatch.setattr(resume, "restore_resume_start", restore)
    runner = Mock(return_value=[])
    monkeypatch.setattr(resume, "run_resume_steps", runner)
    transport = Mock(side_effect=complete)
    monkeypatch.setattr("palinreplay.resume.decision.complete_decision_request", transport)
    options = [] if instruction is None else ["--instruction", instruction]
    assert main(["resume", "source", "--from", "C20", "--max-steps", "2",
                 "--llm-config", str(private_config), "--services-config", "services.json",
                 *options]) == 0
    assert events == ["ready", "request", "cleanup"]
    assert transport.call_count == 1
    assert session.report["decision"]["model_requests"] == 1
    assert session.report["decision"]["remaining_steps"] == 1
    assert session.report["decision"]["complete_nodes"] == 0
    assert session.context == inherited
    assert not session.executor.mock_calls
    assert "test-resume-secret" not in session.report_path.read_text()


@pytest.fixture
def cli_case(tmp_path, monkeypatch, decision_session, private_config):
    import palinreplay.resume.session as sessions
    from palinreplay.agents.mini import MINI_COMMIT, MINI_VERSION
    from palinreplay.core.interfaces import Payload
    from palinreplay.core.models import Attempt, State
    from palinreplay.sandbox.base import ExecResult
    from tests.helpers.recorder_smoke import FakeStateBackend
    from tests.record.test_restore_materials import _record_run
    from tests.resume.test_run_steps import model_response
    from tests.storage.test_store import begin_step, call, candidate

    context = deepcopy(decision_session.context)
    execution = {"cwd": "/testbed", "env": {}, "interpreter": ["bash", "-c"],
                 "timeout": 17, "forward_env": []}
    context["config"]["environment"] = execution
    context["config"].setdefault("agent", {})
    context["config"]["model"]["observation_template"] = "{{output.returncode}}:{{output.output}}"
    context["counters"]["consecutive_format_errors"] = 0
    for key in ("extra_template_vars", "template_vars", "clock", "global_stats", "model_accounting"):
        context.setdefault(key, {})
    context["template_vars"]["model_name"] = "parent-A"
    source = tmp_path / "source-c20" / "record"
    monkeypatch.setattr("palinreplay.storage.btrfs.verification.verify_state", lambda *args: None)
    with RecordStore.create(source) as store:
        run, _, node, *_ = _record_run(store, "parent", payloads={
            "config": Payload.json(context["config"]), "context": Payload.json(context),
            "input_position": Payload.json({"next_message": 42, "adoptions": []}),
        }, agent_name="mini-swe-agent", agent_version=f"{MINI_VERSION}+{MINI_COMMIT}")
        attempt = store.get(Attempt, node.attempt_id)
        state = store.get(State, node.state_id)
        for _ in range(2, 21):
            step = begin_step(store, run, attempt, node)
            model = call(store, step, kind="model")
            node = store.commit_node(candidate(store, step, state, model).model_copy(update={
                "context": store.put_json(context),
                "input_position": store.put_json({"next_message": 42, "adoptions": []}),
            }))
    backend = FakeStateBackend()
    executor = Mock(state="running")
    restored = Mock(report={"cleanup_confirmed": True, "cleanup_errors": []})
    client = Mock()
    events, requests = [], []
    responses = [model_response(1, ["first branch command"]), model_response(2, ["second command"])]
    outcomes = [ExecResult("first", b"actual branch one\n", 0, .1),
                ExecResult("second", b"actual branch two\n", 0, .1)]

    def validate(*args):
        events.append("r1")
        return {"status": "passed"}

    def prepare(command, **kwargs):
        events.append(command)
        return ExecResult(command, b"started", 0, .1)

    def complete(request, credentials):
        requests.append(deepcopy(request))
        events.append(f"model-{len(requests)}")
        assert credentials.model == "openai/branch-model"
        response = responses[len(requests) - 1]
        if isinstance(response, BaseException):
            raise response
        return response

    def execute(request):
        index = executor.execute.call_count - 1
        events.append(f"tool-{index + 1}")
        backend.files["/testbed/branch"] = {"hex": f"branch-{index + 1}".encode().hex(), "mode": 0o644}
        outcome = outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    executor.exec.side_effect = prepare
    executor.execute.side_effect = execute
    executor.close.side_effect = lambda: events.append("executor-close")
    restored.close.side_effect = lambda: events.append("restore-close")
    client.close.side_effect = lambda: events.append("client-close")
    monkeypatch.setattr(sessions, "connect_docker", lambda *args: client)
    monkeypatch.setattr(sessions, "restore_environment", lambda *args, **kwargs: (restored, executor))
    monkeypatch.setattr(sessions, "validate_workspace", validate)
    monkeypatch.setattr("palinreplay.resume.decision.complete_decision_request", complete)
    monkeypatch.setattr("palinreplay.resume.recording.BtrfsStateBackend", lambda *a, **kw: backend)
    output = tmp_path / "cli-branch"
    services = tmp_path / "branch-services.json"
    services.write_text(json.dumps({"services": [
        {"name": name, "start_command": "start " + name, "timeout_sec": 10} for name in ("A", "B")
    ]}))
    argv = ["resume", str(source.parent), "--from", "C20", "--llm-config", str(private_config),
            "--output", str(output), "--services-config", str(services)]
    return SimpleNamespace(source=source, node=node, prefix=context["messages"], output=output,
                           argv=argv, executor=executor, restored=restored, client=client,
                           backend=backend, events=events, requests=requests, responses=responses,
                           outcomes=outcomes)


@pytest.mark.parametrize("budget", [1, 2])
def test_cli_runs_one_branch_with_full_cumulative_context(cli_case, budget, monkeypatch):
    import palinreplay.resume as resume
    from palinreplay.core.models import Node, Run, RunEnd
    from tests.resume.test_multiturn_requests import api_messages

    case = cli_case
    decisions = []
    original_decide = resume.decide_resume_start

    def capture_decision(*args, **kwargs):
        decision = original_decide(*args, **kwargs)
        decisions.append(decision)
        return decision

    decide = Mock(side_effect=capture_decision)
    run = Mock(wraps=resume.run_resume_steps)
    monkeypatch.setattr(resume, "decide_resume_start", decide)
    monkeypatch.setattr(resume, "run_resume_steps", run)
    instruction = "下一步先检查 B 的日志"
    assert main([*case.argv, "--max-steps", str(budget), "--instruction", instruction]) == 0
    assert decide.call_count == run.call_count == 1
    assert decide.call_args.args[0] is run.call_args.args[0]
    assert run.call_args.args[1] is decisions[0]
    assert decide.call_args.kwargs["llm_config"] == run.call_args.kwargs["llm_config"]
    assert len(case.requests) == case.executor.execute.call_count == budget
    assert case.events[:3] == ["r1", "start A", "start B"]
    assert case.events[-3:] == ["executor-close", "restore-close", "client-close"]
    assert len(case.backend.captures) == budget
    assert case.requests[0]["messages"][:42] == api_messages(case.prefix)
    assert case.requests[0]["messages"][42] == {"role": "user", "content": instruction}
    with RecordStore.open(case.output / "record") as store:
        nodes = store.list(Node)
        assert [node.index for node in nodes] == list(range(21, 21 + budget))
        branch, = store.list(Run)
        assert nodes[0].parent_node_id is None
        source = store.read_json(branch.config)["resume"]["source"]
        assert source["node"]["node_id"] == case.node.node_id
        assert store.list(RunEnd)[0].status == "paused"
        assert store.check_integrity().ok
        for index, node in enumerate(nodes):
            messages = store.read_json(node.context)["messages"]
            assert messages[-1]["content"].endswith(f"actual branch {'one' if index == 0 else 'two'}\n")
        if budget == 2:
            assert nodes[1].parent_node_id == nodes[0].node_id
            assert case.requests[1]["messages"] == api_messages(store.read_json(nodes[0].context)["messages"])
    assert all(sum(message.get("content") == instruction for message in request["messages"]) == 1
               for request in case.requests)
    report = json.loads((case.output / "report.json").read_text())
    assert report["recording"]["complete_nodes"] == report["recording"]["total_model_requests"] == budget
    assert report["recording"]["stop_reason"] == "budget_exhausted"
    assert report["cleanup_confirmed"]


@pytest.mark.parametrize("budget,expected_stop", [(1, "budget_exhausted"), (100, "submitted")])
def test_cli_first_text_only_answer_enters_native_correction(cli_case, budget, expected_stop):
    from palinreplay.core.models import Node, RunEnd
    from palinreplay.sandbox.base import ExecResult
    from tests.resume.test_multiturn_requests import api_messages
    from tests.resume.test_run_steps import model_response

    case = cli_case
    reply = model_response(1, [])
    reply.choices[0].finish_reason = "stop"
    reply.choices[0].message.content = "Service B looks healthy."
    case.responses[0] = reply
    case.outcomes[0] = ExecResult("submit", b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\ndone\n", 0, .1)
    instruction = "Inspect B first"
    assert main([*case.argv, "--max-steps", str(budget), "--instruction", instruction]) == 0
    expected_calls = 1 if budget == 1 else 2
    assert len(case.requests) == expected_calls
    assert case.executor.execute.call_count == expected_calls - 1
    assert case.events[:3] == ["r1", "start A", "start B"]
    assert case.events.count("start A") == case.events.count("start B") == 1
    assert case.events[-3:] == ["executor-close", "restore-close", "client-close"]
    with RecordStore.open(case.output / "record", read_only=True) as store:
        nodes = store.list(Node)
        assert [node.index for node in nodes] == list(range(21, 21 + expected_calls))
        first = store.read_json(nodes[0].context)
        assert first["messages"][:42] == case.prefix
        assert first["messages"][42] == {"role": "user", "content": instruction}
        assert "No tool calls found" in first["messages"][-1]["content"]
        assert first["messages"][-1]["extra"]["response"] == reply.model_dump(mode="json")
        assert not nodes[0].tool_execution_ids
        if budget > 1:
            assert case.requests[1]["messages"] == api_messages(first["messages"])
        assert store.list(RunEnd)[0].status == ("paused" if budget == 1 else "completed")
        assert store.check_integrity().ok
    report = json.loads((case.output / "report.json").read_text())
    assert report["status"] == "passed" and report["cleanup_confirmed"]
    assert report["decision"]["status"] == "format_error"
    assert report["recording"]["total_model_requests"] == expected_calls
    assert report["recording"]["stop_reason"] == expected_stop


@pytest.mark.parametrize("outcome,code,status", [
    ("submit", 0, "submitted"), ("budget", 0, "budget_exhausted"),
    ("model_failure", 1, "failed"), ("cancel", 130, "cancelled"),
])
def test_cli_progress_tracks_real_boundaries_and_finishes_after_cleanup(
    cli_case, monkeypatch, outcome, code, status,
):
    import palinreplay.cli.resume as cli
    from palinreplay.core.progress import ProgressReporter
    from palinreplay.sandbox.base import ExecResult

    case, events = cli_case, []

    def observe(event):
        if event.phase == "finished":
            assert case.events[-3:] == ["executor-close", "restore-close", "client-close"]
        events.append(event)

    @contextmanager
    def display(max_steps, mode):
        assert max_steps == 2 and mode == "plain"
        yield ProgressReporter(observe)

    monkeypatch.setattr(cli, "ResumeProgressDisplay", display)
    if outcome == "submit":
        case.outcomes[1] = ExecResult("submit", b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\ndone\n", 0, .1)
    elif outcome in ("model_failure", "cancel"):
        case.responses[1] = (RuntimeError("model unavailable") if outcome == "model_failure"
                             else KeyboardInterrupt())
    assert main([*case.argv, "--max-steps", "2", "--progress", "plain"]) == code
    phases = [event.phase for event in events]
    assert phases.index("restoring") < phases.index("verifying") < phases.index("service-preparation")
    assert phases.index("service-preparation") < phases.index("source-reference") < phases.index("model")
    assert [(event.step, event.request) for event in events if event.phase == "model"] == [(21, 1), (22, 2)]
    complete = 1 if outcome in ("model_failure", "cancel") else 2
    commits = [event for event in events if event.phase == "committed"]
    assert [(event.step, event.completed_steps) for event in commits] == [
        (20 + i, i) for i in range(1, complete + 1)
    ]
    for event in commits:
        positions = {phase: next(i for i, item in enumerate(events)
                                 if item.step == event.step and item.phase == phase)
                     for phase in ("tool", "tool_returned", "capturing", "committing", "committed")}
        assert list(positions.values()) == sorted(positions.values())
    assert len(case.requests) == 2 and case.executor.execute.call_count == complete
    assert phases[-2:] == ["cleanup", "finished"]
    assert events[-1].status == status
    assert events[-1].completed_steps == complete and events[-1].step == 20 + complete


@pytest.mark.parametrize("mode", ["plain", "off"])
def test_cli_progress_mode_preserves_the_final_summary(cli_case, capsys, mode):
    assert main([*cli_case.argv, "--max-steps", "2", "--progress", mode]) == 0
    text = capsys.readouterr()
    assert "Resume FINISHED" in text.out and "Last complete node: C22" in text.out
    assert "\x1b" not in text.err
    if mode == "plain":
        assert "[resume " in text.err and "2/2" in text.err and "源 C20 (引用)" in text.err
    else:
        assert "[resume " not in text.err


def test_progress_observer_failure_does_not_change_resume_execution(cli_case, monkeypatch):
    import palinreplay.cli.resume as cli
    from palinreplay.core.progress import ProgressReporter

    observed = []

    def broken(event):
        observed.append(event.phase)
        raise OSError("terminal disconnected")

    @contextmanager
    def display(*args):
        yield ProgressReporter(broken)

    monkeypatch.setattr(cli, "ResumeProgressDisplay", display)
    assert main([*cli_case.argv, "--max-steps", "2"]) == 0
    assert observed == ["preparing"]
    assert len(cli_case.requests) == cli_case.executor.execute.call_count == 2
    report = json.loads((cli_case.output / "report.json").read_text())
    assert report["recording"]["complete_nodes"] == 2 and report["cleanup_confirmed"]


def test_cli_budget_100_stops_when_the_second_round_submits(cli_case):
    from palinreplay.core.models import Node, RunEnd
    from palinreplay.sandbox.base import ExecResult

    cli_case.outcomes[1] = ExecResult("submit", b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\ndone\n", 0, .1)
    assert main([*cli_case.argv, "--max-steps", "100"]) == 0
    assert len(cli_case.requests) == cli_case.executor.execute.call_count == 2
    with RecordStore.open(cli_case.output / "record") as store:
        assert [node.index for node in store.list(Node)] == [21, 22]
        assert store.list(RunEnd)[0].status == "completed"
    report = json.loads((cli_case.output / "report.json").read_text())
    assert report["recording"]["stop_reason"] == "submitted"


def test_cli_gives_a_nonzero_tool_result_to_the_next_request(cli_case):
    from palinreplay.sandbox.base import ExecResult

    cli_case.outcomes[0] = ExecResult("first", b"ordinary tool failure\n", 7, .1)
    assert main([*cli_case.argv, "--max-steps", "2"]) == 0
    assert cli_case.requests[1]["messages"][-1]["content"] == "7:ordinary tool failure\n"
    report = json.loads((cli_case.output / "report.json").read_text())
    assert report["recording"]["complete_nodes"] == 2


def test_cli_keeps_an_incomplete_group_tail_without_publishing_a_node(cli_case):
    from palinreplay.core.models import Node, RunEnd, ToolGroup
    from palinreplay.sandbox.base import ExecResult
    from tests.resume.test_run_steps import model_response

    cli_case.responses[1] = model_response(2, ["submit", "never executed"])
    cli_case.outcomes[1] = ExecResult("submit", b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\ndone\n", 0, .1)
    assert main([*cli_case.argv, "--max-steps", "2"]) == 3
    assert len(cli_case.requests) == cli_case.executor.execute.call_count == 2
    with RecordStore.open(cli_case.output / "record") as store:
        assert [node.index for node in store.list(Node)] == [21]
        assert store.list(RunEnd)[0].status == "incomplete"
        assert len(store.list(ToolGroup)[-1].tool_call_ids) == 2
        assert store.check_integrity().ok
    report = json.loads((cli_case.output / "report.json").read_text())
    assert report["recording"]["stop_reason"] == "incomplete_tool_group"


@pytest.mark.parametrize("request_number", [1, 2])
def test_cli_model_failure_retains_the_last_complete_node(cli_case, request_number):
    from palinreplay.core.models import Node

    cli_case.responses[request_number - 1] = RuntimeError("model request failed")
    assert main([*cli_case.argv, "--max-steps", "2"]) == 1
    assert len(cli_case.requests) == request_number
    assert cli_case.executor.execute.call_count == request_number - 1
    report = json.loads((cli_case.output / "report.json").read_text())
    assert report["cleanup_confirmed"]
    if request_number == 1:
        assert "recording" not in report and not (cli_case.output / "record").exists()
        assert report["decision"]["complete_nodes"] == 0
    else:
        assert report["recording"]["last_complete_node"]["index"] == 21
        with RecordStore.open(cli_case.output / "record") as store:
            assert [node.index for node in store.list(Node)] == [21]


@pytest.mark.parametrize("stage", ["first-model", "later-model", "tool", "snapshot", "preparation"])
def test_cli_pure_cancellation_returns_130_and_cleans_up(cli_case, stage):
    interrupted = KeyboardInterrupt("user cancelled")
    if stage == "first-model":
        cli_case.responses[0] = interrupted
    elif stage == "later-model":
        cli_case.responses[1] = interrupted
    elif stage == "tool":
        cli_case.outcomes[1] = interrupted
    elif stage == "snapshot":
        cli_case.backend.capture = Mock(side_effect=interrupted)
    else:
        cli_case.executor.exec.side_effect = interrupted
    assert main([*cli_case.argv, "--max-steps", "2"]) == 130
    report = interrupted.resume_report
    assert report["status"] == "failed" and report["cleanup_confirmed"]
    assert report["cleanup_errors"] == []
    assert cli_case.events[-3:] == ["executor-close", "restore-close", "client-close"]
    if stage in ("later-model", "tool"):
        assert report["recording"]["complete_nodes"] == 1
        assert report["recording"]["last_complete_node"]["index"] == 21


def test_cli_sigterm_uses_existing_cancellation_and_restores_handler(cli_case, monkeypatch):
    import signal

    import palinreplay.resume.decision as decision

    original, previous = decision.complete_decision_request, signal.getsignal(signal.SIGTERM)

    def interrupt(request, credentials):
        if len(cli_case.requests) == 1:
            signal.raise_signal(signal.SIGTERM)
        return original(request, credentials)

    monkeypatch.setattr(decision, "complete_decision_request", interrupt)
    assert main([*cli_case.argv, "--max-steps", "2"]) == 130
    assert signal.getsignal(signal.SIGTERM) == previous
    assert cli_case.events[-3:] == ["executor-close", "restore-close", "client-close"]


@pytest.mark.parametrize("fault", ["snapshot", "after-publication"])
def test_cli_recording_failure_uses_actual_publication_facts(cli_case, monkeypatch, fault):
    from palinreplay.core.models import Node

    if fault == "snapshot":
        cli_case.backend.capture = Mock(side_effect=OSError("snapshot unavailable"))
    else:
        commit = RecordStore.commit_node

        def publish_then_fail(store, node):
            commit(store, node)
            raise OSError("after publication")

        monkeypatch.setattr(RecordStore, "commit_node", publish_then_fail)
    assert main([*cli_case.argv, "--max-steps", "2"]) == 1
    report = json.loads((cli_case.output / "report.json").read_text())
    with RecordStore.open(cli_case.output / "record") as store:
        expected = 0 if fault == "snapshot" else 1
        assert len(store.list(Node)) == report["recording"]["complete_nodes"] == expected
    assert report["cleanup_confirmed"]


def test_cli_submission_with_cleanup_failure_is_failure_and_keeps_nodes(cli_case):
    from palinreplay.core.models import Node
    from palinreplay.sandbox.base import ExecResult

    cli_case.outcomes[1] = ExecResult("submit", b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\ndone\n", 0, .1)
    cli_case.executor.close.side_effect = OSError("executor close failed")
    assert main([*cli_case.argv, "--max-steps", "100"]) == 1
    report = json.loads((cli_case.output / "report.json").read_text())
    assert report["recording"]["stop_reason"] == "submitted"
    assert not report["cleanup_confirmed"] and report["cleanup_errors"]
    with RecordStore.open(cli_case.output / "record") as store:
        assert [node.index for node in store.list(Node)] == [21, 22]
    cli_case.restored.close.assert_called_once()
    cli_case.client.close.assert_called_once()


@pytest.mark.parametrize("fault", ["cleanup", "recording", "report", "decision-evidence"])
def test_cli_known_failure_takes_priority_over_cancellation(cli_case, monkeypatch, fault, capsys):
    interrupted = KeyboardInterrupt("user cancelled")
    cli_case.responses[1 if fault != "decision-evidence" else 0] = interrupted
    if fault == "cleanup":
        cli_case.executor.close.side_effect = OSError("cleanup unavailable")
    elif fault == "recording":
        from palinreplay.record.recorder import Recorder

        fail = Mock(side_effect=OSError("recorder evidence unavailable"))
        monkeypatch.setattr(Recorder, "_failure", fail)
    elif fault == "report":
        import palinreplay.resume.report as reports

        write = reports.write_json

        def fail_final(path, data, **kwargs):
            if data.get("phase") == "complete":
                raise OSError("final report unavailable")
            return write(path, data, **kwargs)

        monkeypatch.setattr(reports, "write_json", fail_final)
    else:
        import palinreplay.resume.decision as decision

        write = decision.write_json

        def fail_interrupted(path, data, **kwargs):
            if data.get("decision", {}).get("status") == "interrupted":
                raise OSError("decision evidence unavailable")
            return write(path, data, **kwargs)

        monkeypatch.setattr(decision, "write_json", fail_interrupted)
    assert main([*cli_case.argv, "--max-steps", "2"]) == 1
    report = interrupted.resume_report
    assert report["caller"]["error_type"] == "KeyboardInterrupt"
    if fault == "cleanup":
        assert report["cleanup_errors"] and not report["cleanup_confirmed"]
    elif fault == "report":
        assert report["report_error"] and report["cleanup_confirmed"]
    else:
        assert interrupted.__notes__
        assert report["cleanup_confirmed"]
    output = capsys.readouterr()
    assert "cancelled" in output.err and "also has" in output.err
    cli_case.restored.close.assert_called_once()
    cli_case.client.close.assert_called_once()


def test_cli_summary_matches_the_report_and_runs_after_cleanup(cli_case, monkeypatch, capsys):
    import builtins

    original_print = builtins.print

    def observe_summary(*args, **kwargs):
        if args and str(args[0]).startswith("Resume FINISHED"):
            cli_case.events.append("summary")
        original_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", observe_summary)
    assert main([*cli_case.argv, "--max-steps", "2"]) == 0
    report = json.loads((cli_case.output / "report.json").read_text())
    output = capsys.readouterr()
    assert cli_case.events[-4:] == ["executor-close", "restore-close", "client-close", "summary"]
    for text in ("Source: C20", f"Branch Run: {report['recording']['run_id']}",
                 "New complete nodes: 2", "Last complete node: C22", "Model requests: 2",
                 "Tool executions (attempts): 2", "Stop reason: budget_exhausted", "Cleanup: completed",
                 f"Report: {cli_case.output / 'report.json'}", f"Branch Store: {cli_case.output / 'record'}"):
        assert text in output.out
    assert "test-resume-secret" not in output.out + output.err
    assert "test-resume-secret" not in (cli_case.output / "report.json").read_text()
    assert "\x1b" not in output.out + output.err


def test_cli_first_request_failure_reports_zero_nodes_and_the_source(cli_case, capsys):
    cli_case.responses[0] = RuntimeError("model unavailable")
    assert main([*cli_case.argv, "--max-steps", "2"]) == 1
    output = capsys.readouterr()
    for text in ("Resume FAILED", "Branch Run: not created", "New complete nodes: 0",
                 "Last complete node: source C20", "Model requests: 1", "Tool executions (attempts): 0",
                 "Stop reason: failed", "Cleanup: completed"):
        assert text in output.out
    assert "recording/" not in output.out


def test_cli_later_failure_displays_saved_node_and_failure(cli_case, capsys):
    cli_case.responses[1] = RuntimeError("second request failed")
    assert main([*cli_case.argv, "--max-steps", "2"]) == 1
    output = capsys.readouterr()
    for text in ("New complete nodes: 1", "Last complete node: C21", "Model requests: 2",
                 "Failure phase: model", "Resume FAILED", "Stop reason: failed"):
        assert text in output.out


def test_cli_unreadable_final_progress_is_unknown_not_zero(cli_case, monkeypatch, capsys):
    monkeypatch.setattr("palinreplay.resume.recording._persisted_progress",
                        Mock(side_effect=OSError("progress unreadable")))
    assert main([*cli_case.argv, "--max-steps", "1"]) == 1
    output = capsys.readouterr()
    assert "New complete nodes: unknown" in output.out
    assert "Last complete node: unknown (could not confirm saved progress)" in output.out
    assert "New complete nodes: 0" not in output.out
    assert "Last complete node: C21" not in output.out
    assert "Model requests: 1" in output.out


def test_cli_final_report_failure_uses_latest_memory_after_cleanup(cli_case, monkeypatch, capsys):
    import palinreplay.resume.report as reports

    write = reports.write_json
    error = OSError("final report unavailable")

    def fail_final(path, data, **kwargs):
        if data.get("phase") == "complete":
            raise error
        return write(path, data, **kwargs)

    monkeypatch.setattr(reports, "write_json", fail_final)
    assert main([*cli_case.argv, "--max-steps", "2"]) == 1
    assert error.resume_report["cleanup_confirmed"]
    assert not json.loads((cli_case.output / "report.json").read_text())["cleanup_confirmed"]
    output = capsys.readouterr()
    for text in ("Resume FAILED", "New complete nodes: 2", "Last complete node: C22",
                 "Cleanup: completed", "Report write: failed; summary uses retained in-memory evidence"):
        assert text in output.out


def test_cli_does_not_present_preexisting_reports_as_its_own(restoration, private_config, capsys):
    restoration.output.mkdir()
    (restoration.output / "report.json").write_text('{"run_id": "old-run"}')
    assert main(_restoration_args(restoration, private_config)) == 1
    output = capsys.readouterr()
    assert "Report:" not in output.out and "old-run" not in output.out
    assert "output already exists" in output.err


def test_cli_early_error_does_not_claim_a_report_was_generated(private_config, capsys):
    assert main(["resume", "missing-source", "--from", "C20",
                 "--llm-config", str(private_config)]) == 1
    output = capsys.readouterr()
    assert "Report:" not in output.out
    assert "error:" in output.err
