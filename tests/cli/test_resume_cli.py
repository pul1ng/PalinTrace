from pathlib import Path

import pytest

from palinreplay.cli import main
from palinreplay.cli.app import build_parser
from palinreplay.storage.store import RecordStore
from tests.resume.test_start import history


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
                   "--instruction", "--services-config", "--output"):
        assert option in help_text
    assert "default: 20" in help_text
    assert "including the first request" in help_text
    assert "submit early" in help_text
    assert "--to" not in help_text and "--progress" not in help_text


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
