import json
import os
import signal
from types import SimpleNamespace

import pytest

from palinreplay.cli import main
from palinreplay.cli.progress import RecordProgressDisplay
from palinreplay.core.models import Failure, Node
from palinreplay.record.inspect import inspect_record
from palinreplay.record.run import RunOptions, run_record
from palinreplay.sandbox.base import SandboxCleanupError
from palinreplay.storage.store import RecordStore
from tests.agents.test_mini_agent import Capture, Sandbox, Transport, response
from tests.agents.test_mini_agent import deterministic as deterministic
from tests.sandbox.test_sandbox import package as package


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    import palinreplay.record.run as runner

    state = SimpleNamespace(
        sandboxes=[],
        captures=[],
        clients=[],
        cleanup_error=False,
        start_error=False,
        capture_error=None,
        capture_exception=None,
    )

    class OwnedSandbox(Sandbox):
        def __init__(self, binding, *, preload, client, network_mode):
            super().__init__()
            self.closed = False
            self.name, self.run_id = "owned", "sandbox-run"
            self.network = network_mode
            state.sandboxes.append(self)

        def start(self):
            if state.start_error:
                raise RuntimeError("start failed https://private.invalid/?key=DO-NOT-LOG")
            return SimpleNamespace(container_id="owned", actual_image_id="sha256:fixture")

        def close(self):
            self.closed = True
            if state.cleanup_error:
                raise SandboxCleanupError("private cleanup detail DO-NOT-LOG")

    class Backend:
        def __init__(self, sandbox, *, public_env_keys):
            self.public_env_keys = frozenset(public_env_keys)
            self.capture_fixture = Capture(state.sandboxes[-1])
            state.captures.append(self)

        def capture(self, store, boundary, previous):
            if boundary.index == state.capture_error:
                if state.capture_exception:
                    raise state.capture_exception
                raise OSError("injected disk full")
            return self.capture_fixture(store, boundary, previous)

    def connect(timeout):
        client = SimpleNamespace(
            containers=SimpleNamespace(
                get=lambda _: SimpleNamespace(attrs={"Config": {"Env": ["PATH=/bin"]}})
            ),
            closed=False,
        )
        client.close = lambda: setattr(client, "closed", True)
        state.clients.append(client)
        return client

    monkeypatch.setattr(runner, "connect_docker", connect)
    monkeypatch.setattr(runner, "BtrfsSandbox", OwnedSandbox)
    monkeypatch.setattr(runner, "BtrfsStateBackend", Backend)
    base = SimpleNamespace(
        path=tmp_path / "base",
        metadata=SimpleNamespace(launch=SimpleNamespace(environment={})),
    )
    selected = SimpleNamespace(
        base=base, describe=lambda: {"backend": "btrfs-image.v1"}, attach=lambda store: None,
    )
    monkeypatch.setattr(runner.BtrfsPreloadRecord, "select", lambda path, binding: selected)
    return state


def test_run_closes_resources_reopens_and_measures_storage(package, tmp_path, runtime):
    transport = Transport([response("write"), response("submit")])
    output = tmp_path / "run"
    report = run_record(
        package,
        output,
        completion=transport,
        options=RunOptions(preload="base", network="none"),
    )
    assert report["status"] == "recorded" and report["exit_code"] == 0, report
    assert report["cleanup_confirmed"] and not report["cleanup_errors"]
    assert runtime.clients[0].closed and runtime.sandboxes[0].closed
    assert runtime.sandboxes[0].network == "none"
    assert len(transport.requests) == 2
    assert report["verification"]["ok"] and report["runs"][0]["record_complete"]
    assert report["storage"]["node_count"] == 3
    assert report["storage"]["state_count"] == 2
    assert all(
        row["store_bytes_after_capture"] >= row["store_bytes_before_capture"]
        for row in report["capture_boundaries"]
    )
    assert report["duration_sec"] >= report["agent_run_sec"] >= 0
    assert report["boundary_metrics"]["initial"]["count"] == 1
    assert report["boundary_metrics"]["steps"]["count"] == 2
    assert report["boundary_metrics"]["steps"]["io"]["verification_calls"] > 0
    assert json.loads((output / "report.json").read_text()) == report
    assert report["debug_timing"]["timeline_complete"]
    assert [step["index"] for step in assert_flow_accounting(report)["steps"]] == [0, 1, 2]


@pytest.mark.parametrize("debug", [False, True])
def test_timings_cover_requests_retries_tools_boundaries_and_final_check(
    package, tmp_path, runtime, debug
):
    output = tmp_path / "run"
    finished = []
    events = []

    def observe(event):
        events.append(event)
        if event.phase == "finished":
            finished.append(json.loads((output / "report.json").read_text()))

    transport = Transport([
        RuntimeError("PRIVATE-DO-NOT-LOG"), response("write", "bad"), response("submit")
    ])
    report = run_record(
        package, output, completion=transport, progress=observe, debug=debug,
        options=RunOptions(preload="base", retry_attempts=2),
    )
    assert report["status"] == "recorded" and report["verification"]["ok"]
    timing = report["debug_timing"]
    assert finished == [report]
    flow = assert_flow_accounting(report)
    assert [step["index"] for step in flow["steps"]] == [0, 1, 2]
    if debug:
        assert_live_flow_matches_report(events, flow)
    else:
        assert not any(event.phase.startswith("flow_") for event in events)
    assert timing["timeline_complete"] and "PRIVATE" not in str(timing)
    assert "command" not in str(timing) and "messages" not in str(timing)
    totals = timing["totals"]
    for name in (
        "prepare.task", "sandbox.start", "agent.configure", "call.prepare", "agent.adopt",
        "call.save_result", "artifact.sync_file", "catalog.commit", "record.verify",
        "cleanup.sandbox", "cleanup.store", "cleanup.client",
    ):
        assert totals[name]["count"] >= 1
        assert totals[name]["duration_sec"] >= totals[name]["self_sec"] >= 0
    assert totals["model.request"]["count"] == 3
    assert totals["model.request"]["raised"] == 1
    assert totals["model.retry_wait"]["count"] == 1
    assert totals["tool.execute"]["count"] == 3
    boundaries = [row for row in timing["spans"] if row["name"] == "node.capture_commit"]
    assert [row["step"] for row in boundaries] == [0, 1, 2]
    assert all(row["status"] == "ok" for row in boundaries)
    for row in timing["spans"]:
        if row["parent_id"] is not None:
            parent = timing["spans"][row["parent_id"]]
            assert parent["offset_sec"] <= row["offset_sec"]
            assert row["offset_sec"] + row["duration_sec"] <= (
                parent["offset_sec"] + parent["duration_sec"] + 1e-6
            )


@pytest.mark.parametrize("debug", [False, True])
def test_capture_failure_is_timed_and_never_published(package, tmp_path, runtime, debug):
    runtime.capture_error = 1
    report = run_record(
        package,
        tmp_path / "run",
        completion=Transport([response("write")]),
        debug=debug,
        options=RunOptions(preload="base"),
    )
    assert report["status"] == "failed" and report["cleanup_confirmed"]
    boundaries = [
        row for row in report["debug_timing"]["spans"] if row["name"] == "node.capture_commit"
    ]
    assert [(row["step"], row["status"]) for row in boundaries] == [(0, "ok"), (1, "raised")]
    assert report["storage"]["node_count"] == 1


def assert_flow_accounting(report):
    flow = report["debug_timing"]["flow"]
    assert sum(p["duration_sec"] for p in flow["phases"]) == pytest.approx(flow["duration_sec"])
    assert flow["duration_sec"] <= report["debug_timing"]["totals"]["record.total"]["duration_sec"]
    for phase in flow["phases"]:
        assert phase["other_sec"] >= 0
        assert phase["measured_substeps_sec"] + phase["other_sec"] == pytest.approx(
            phase["duration_sec"]
        )
    for group in flow["steps"]:
        capture = group["capture"]
        if capture["duration_sec"] is not None:
            assert capture["other_sec"] >= 0
            assert sum(r["duration_sec"] or 0 for r in capture["substeps"]) + capture[
                "other_sec"
            ] == pytest.approx(capture["duration_sec"])
    return flow


def assert_live_flow_matches_report(events, flow):
    finished = [e for e in events if e.phase == "flow_end"]

    def check_rows(rows, candidates):
        for row in rows:
            matches = [e for e in candidates if e.code == row["id"] or e.subcode == row["id"]]
            assert len(matches) == row["count"]
            if row["duration_sec"] is not None:
                assert sum(e.duration_sec for e in matches) == pytest.approx(row["duration_sec"])
                assert sum(e.inclusive_sec for e in matches) == pytest.approx(row["inclusive_sec"])
                assert sum(e.status == "raised" for e in matches) == row["raised"]
            if "parts" in row:
                check_rows(row["parts"], candidates)
                if row["duration_sec"] is not None:
                    assert sum(p["duration_sec"] or 0 for p in row["parts"]) + row[
                        "unclassified_sec"
                    ] == pytest.approx(row["duration_sec"])

    for index, phase in enumerate(flow["phases"], 1):
        check_rows(phase["substeps"], finished)
        ends = [e for e in events if e.phase == "flow_phase"
                and e.status == "finished" and e.code == str(index)]
        assert len(ends) <= 1
        assert sum(e.duration_sec for e in ends) == pytest.approx(phase["duration_sec"])
    for group in flow["steps"]:
        candidates = [e for e in finished if e.step == group["index"]]
        check_rows(group["substeps"], candidates)
        check_rows(group["capture"]["substeps"], candidates)
    assert "PRIVATE" not in str(events)


def test_flow_timings_cover_document_steps_and_capped_debug_trace(
    package, tmp_path, runtime, monkeypatch, capsys
):
    import palinreplay.record.run as runner
    from palinreplay.cli.record import _timing_summary
    from palinreplay.diagnostics.timing import debug_timing

    monkeypatch.setattr(runner, "debug_timing", lambda enabled: debug_timing(enabled, max_spans=2))
    events = []
    report = run_record(
        package,
        tmp_path / "run",
        debug=True,
        progress=events.append,
        completion=Transport([response("write", "bad"), response("submit")]),
        options=RunOptions(preload="base"),
    )
    assert report["status"] == "recorded"
    assert not report["debug_timing"]["timeline_complete"]
    flow = assert_flow_accounting(report)
    assert_live_flow_matches_report(events, flow)
    assert flow["step_details_complete"]
    assert [s["index"] for s in flow["steps"]] == [0, 1, 2]
    for phase in flow["phases"]:
        assert all(r["measurement"] == "measured" for r in phase["substeps"])
    assert json.loads((tmp_path / "run/report.json").read_text())["debug_timing"]["flow"] == flow
    _timing_summary(report["debug_timing"])
    output = capsys.readouterr().out
    assert "1.5 构造初始 Agent 上下文" in output
    assert "Step 2:" in output and "S6=" in output and "未执行/未埋点" in output
    for part in ("1.2.1 读取任务包", "1.2.2 检查并加载 Agent", "1.2.3 读取模型连接配置",
                 "2.6.1 适配器完整性预检", "2.6.2 Recorder 提交前检查"):
        assert part in output
    starts = [e for e in events if e.phase == "flow_start"]
    assert [e.subcode for e in starts if e.code == "1.2"] == ["1.2.1", "1.2.2", "1.2.3"]
    assert [e.subcode for e in starts if e.code == "1.3"] == ["1.3.1", "1.3.2", "1.3.3"]
    assert [e.subcode for e in starts if e.code == "1.4"] == [
        "1.4.1", "1.4.2", "1.4.3", "1.4.4",
    ]
    assert [(e.step, e.tool, e.tools) for e in starts if e.code == "2.4"] == [
        (1, 1, 2), (1, 2, 2), (2, 1, 1),
    ]
    first = next(e for e in starts if e.code == "2.1" and e.subcode == "2.1.1")
    nested = next(e for e in starts if e.code == "2.1" and e.subcode == "2.1.2")
    assert nested.depth > first.depth

    # Historical reports have only main categories, without part rows.
    def old_format(value):
        if isinstance(value, dict):
            return {k: old_format(v) for k, v in value.items() if k not in {"parts", "unclassified_sec"}}
        if isinstance(value, list):
            return [old_format(v) for v in value]
        return value

    _timing_summary(old_format(report["debug_timing"]))
    old_output = capsys.readouterr().out
    assert "1.2 加载任务与模型配置" in old_output and "1.2.1" not in old_output


@pytest.mark.parametrize("case", ["budget", "format", "incomplete", "cancel", "c0", "c1", "start", "cleanup"])
def test_flow_timings_on_stop_paths(package, tmp_path, runtime, case):
    runtime.capture_error = {"c0": 0, "c1": 1}.get(case)
    runtime.start_error = case == "start"
    runtime.cleanup_error = case == "cleanup"
    replies = {
        "budget": [response("write")], "format": [response(malformed="not-json")],
        "incomplete": [response("submit", "write")], "cancel": [response("cancel")],
        "c0": [], "c1": [response("write")], "start": [], "cleanup": [response("submit")],
    }
    events = []
    report = run_record(
        package,
        tmp_path / "run",
        debug=True,
        progress=events.append,
        completion=Transport(replies[case]),
        options=RunOptions(preload="base", max_steps=1),
    )
    flow = assert_flow_accounting(report)
    assert_live_flow_matches_report(events, flow)
    assert flow["phases"][2]["duration_sec"] > 0
    if case in {"start", "c0"}:
        assert flow["phases"][1]["duration_sec"] == 0
    if case == "budget":
        assert report["storage"]["node_count"] == 2
        check_only = flow["steps"][-1]
        assert check_only["index"] == 2 and check_only["capture"]["duration_sec"] is None
        assert next(r for r in check_only["substeps"] if r["id"] == "2.2")["duration_sec"] is None


@pytest.mark.parametrize("interrupt_code", ["1.5", "2.2"])
def test_cancel_at_live_substep_start_still_finalizes_and_cleans(
    package, tmp_path, runtime, interrupt_code
):
    events, cancelled = [], False

    def observe(event):
        nonlocal cancelled
        events.append(event)
        if not cancelled and event.phase == "flow_start" and event.code == interrupt_code:
            cancelled = True
            raise KeyboardInterrupt("PRIVATE interrupt")

    report = run_record(
        package,
        tmp_path / "run",
        debug=True,
        progress=observe,
        completion=Transport([]),
        options=RunOptions(preload="base"),
    )
    assert cancelled and report["status"] == "cancelled" and report["exit_code"] == 130
    assert report["cleanup_confirmed"] and report["runs"][0]["status"] == "cancelled"
    assert runtime.clients[0].closed and runtime.sandboxes[0].closed
    assert report["storage"]["node_count"] == (0 if interrupt_code == "1.5" else 1)
    assert_live_flow_matches_report(events, assert_flow_accounting(report))


@pytest.mark.parametrize("fault", ["initial_context", "commit"])
def test_flow_context_cleanup_after_initialization_or_commit_error(
    package, tmp_path, runtime, monkeypatch, fault
):
    from palinreplay.agents.mini import load_native

    if fault == "initial_context":
        def fail_template(*args, **kwargs):
            raise ValueError("PRIVATE-INITIAL-CONTEXT")

        monkeypatch.setattr(load_native().RecordingAgent, "_render_template", fail_template)
    else:
        commit = RecordStore.commit_node

        def fail_commit(self, node):
            if node.index:
                raise OSError("PRIVATE-COMMIT")
            return commit(self, node)

        monkeypatch.setattr(RecordStore, "commit_node", fail_commit)
    report = run_record(
        package,
        tmp_path / "run",
        debug=True,
        completion=Transport([response("write")]),
        options=RunOptions(preload="base"),
    )
    assert report["status"] == "failed" and report["cleanup_confirmed"]
    flow = assert_flow_accounting(report)
    assert "PRIVATE" not in str(flow)
    if fault == "initial_context":
        row = next(r for r in flow["phases"][0]["substeps"] if r["id"] == "1.5")
        assert row["raised"] == 1 and report["storage"]["node_count"] == 0
    else:
        capture = flow["steps"][-1]["capture"]
        assert capture["raised"] == 1 and report["storage"]["node_count"] == 1
        assert next(r for r in capture["substeps"] if r["id"] == "S6")["raised"] == 1


@pytest.mark.parametrize("case", ["budget", "incomplete", "timeout", "cancel", "c0", "c1", "start"])
def test_run_exit_status_and_intact_prefix(package, tmp_path, runtime, case):
    responses = {
        "budget": [response("write")],
        "incomplete": [response("submit", "write")],
        "timeout": [response("timeout")],
        "cancel": [response("cancel")],
        "c0": [],
        "c1": [response("write")],
        "start": [],
    }
    runtime.capture_error = {"c0": 0, "c1": 1}.get(case)
    runtime.start_error = case == "start"
    transport = Transport(responses[case])
    output = tmp_path / "run"
    report = run_record(
        package,
        output,
        completion=transport,
        options=RunOptions(preload="base", max_steps=1),
    )
    assert runtime.clients[0].closed and runtime.sandboxes[0].closed
    if case == "budget":
        assert report["exit_code"] == 0 and report["run_end_status"] == "paused"
    elif case == "incomplete":
        assert report["exit_code"] == 3 and report["status"] == "incomplete"
    elif case == "cancel":
        assert report["exit_code"] == 130 and report["status"] == "cancelled"
    else:
        assert report["exit_code"] == 1 and report["status"] == "failed"
    if case == "start":
        assert not (output / "record").exists()
    else:
        view = inspect_record(output / "record", check=True)
        assert view["verification"]["ok"]
        count = 0 if case == "c0" else 2 if case == "budget" else 1
        assert len(view["runs"][0]["nodes"]) == count
    assert len(transport.requests) <= 1
    assert "DO-NOT-LOG" not in (output / "report.json").read_text()


def test_primary_and_cleanup_errors_remain_separate(package, tmp_path, runtime):
    runtime.cleanup_error = True
    report = run_record(
        package,
        tmp_path / "run",
        completion=Transport([response("timeout")]),
        options=RunOptions(preload="base"),
    )
    assert report["error"]["type"] == "SandboxTimeout"
    assert report["cleanup_errors"][0]["type"] == "SandboxCleanupError"
    assert not report["cleanup_confirmed"] and report["exit_code"] == 1
    with RecordStore.open(tmp_path / "run/record") as store:
        assert len(store.list(Node)) == 1
        assert any(f.category == "cleanup" for f in store.list(Failure))


def test_report_write_failure_stops_before_model_and_still_cleans(
    package, tmp_path, runtime, monkeypatch
):
    import palinreplay.record.run as runner

    original = runner.write_json
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("report disk failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(runner, "write_json", fail_once)
    transport = Transport([])
    report = run_record(
        package, tmp_path / "run", completion=transport, options=RunOptions(preload="base")
    )
    assert report["error"]["type"] == "OSError"
    assert report["cleanup_confirmed"] and runtime.clients[0].closed
    assert not transport.requests


@pytest.mark.parametrize(
    "options",
    [
        RunOptions(preload="base", max_steps=0),
        RunOptions(preload="base", max_tokens=-1),
        RunOptions(preload="base", time_limit=float("nan")),
        RunOptions(preload="base", cost_limit=-1),
        RunOptions(preload="base", retry_attempts=0),
    ],
)
def test_invalid_options_fail_before_resources_or_output(package, tmp_path, runtime, options):
    with pytest.raises(ValueError):
        run_record(package, tmp_path / "run", completion=Transport([]), options=options)
    assert not runtime.clients and not (tmp_path / "run").exists()


def test_existing_run_is_not_reused_or_deleted(package, tmp_path, runtime):
    output = tmp_path / "run"
    output.mkdir()
    (output / "user").write_text("keep")
    with pytest.raises(FileExistsError):
        run_record(
            package, output, completion=Transport([]), options=RunOptions(preload="base")
        )
    assert (output / "user").read_text() == "keep" and not runtime.clients


def test_cli_run_wires_limits_and_never_logs_private_config(
    package, tmp_path, runtime, monkeypatch, capsys
):
    import palinreplay.record.run as runner

    real = runner.run_record
    seen = []

    def run(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs, completion=Transport([response("write")]))

    monkeypatch.setattr(runner, "run_record", run)
    assert (
        main(
            [
                "record",
                "run", "--preload", "base",
                str(package),
                "--output",
                str(tmp_path / "run"),
                "--max-steps",
                "1",
                "--max-tokens",
                "32",
                "--time-limit",
                "300",
                "--network",
                "none",
                "--debug",
            ]
        )
        == 0
    )
    assert seen[0]["options"].max_tokens == 32
    assert seen[0]["debug"] is True
    view = inspect_record(tmp_path / "run/record", node="C1")
    config = view["selected_node"]["context_content"]["value"]["config"]
    assert config["agent"]["wall_time_limit_seconds"] == 300
    output = capsys.readouterr()
    assert "Report:" in output.out and "Capturing C0" in output.err
    assert "Debug timings" in output.out and "C1: capture/commit" in output.out
    assert "1.1 检查运行参数: 开始" in output.err
    assert "Step 1 · 2.1" in output.err and "Step 2 · 2.1" in output.err
    assert "Step 2 · 2.2" not in output.err and "C2 · S1" not in output.err
    assert "C0 · S1" in output.err and "C1 · S6" in output.err
    assert "本段耗时" in output.err and "3.3 重新打开 Store 检查: 结束" in output.err
    assert output.err.count("1.2.1 读取任务包: 开始") == 1
    assert output.err.count("1.2.2 检查并加载 Agent: 开始") == 1
    assert output.err.count("1.2.3 读取模型连接配置: 开始") == 1
    assert "1.2 加载任务与模型配置: 开始" not in output.err
    assert "2.4 执行工具并录制 · 工具 1/1" in output.err
    assert (
        main(
            [
                "record",
                "run",
                "--preload",
                "base",
                str(package),
                "--output",
                str(tmp_path / "bad"),
                "--max-steps",
                "0",
            ]
        )
        == 1
    )


def test_cli_sigterm_uses_cancellation_and_restores_handler(
    package, tmp_path, runtime, monkeypatch
):
    import palinreplay.record.run as runner

    real = runner.run_record
    previous = signal.getsignal(signal.SIGTERM)

    def completion(**request):
        os.kill(os.getpid(), signal.SIGTERM)
        pytest.fail("SIGTERM must stop the query")

    monkeypatch.setattr(
        runner, "run_record", lambda *a, **kw: real(*a, **kw, completion=completion)
    )
    assert (
        main(
            [
                "record",
                "run",
                "--preload",
                "base",
                str(package),
                "--output",
                str(tmp_path / "run"),
            ]
        )
        == 130
    )
    report = json.loads((tmp_path / "run/report.json").read_text())
    assert report["status"] == "cancelled" and report["cleanup_confirmed"]
    assert signal.getsignal(signal.SIGTERM) == previous
    assert inspect_record(tmp_path / "run/record")["runs"][0]["status"] == "cancelled"


def test_public_provenance_is_kept_without_evaluation(package, tmp_path, runtime):
    run_record(
        package,
        tmp_path / "run",
        completion=Transport([response("submit")]),
        options=RunOptions(preload="base"),
    )
    from palinreplay.core.models import Run

    with RecordStore.open(tmp_path / "run/record") as store:
        config = store.read_json(store.list(Run)[0].config)
    metadata = config["record_metadata"]
    assert metadata["repository"]["base_commit"] == "a" * 40
    assert len(metadata["dataset_manifest_sha256"]) == 64
    assert "evaluation" not in json.dumps(config) and "synthetic gold patch" not in json.dumps(
        config
    )


def test_progress_follows_real_commits_retries_and_complete_tool_groups(package, tmp_path, runtime):
    events = []
    published = []
    final_reports = []
    output = tmp_path / "run"
    transport = Transport(
        [
            RuntimeError("private SDK error DO-NOT-LOG"),
            response("write", "bad"),
            response("submit"),
        ]
    )

    def observe(event):
        events.append(event)
        if event.phase == "committed":
            # The event is after publication, not just after capture returned.
            with RecordStore.open(output / "record") as store:
                published.append((event.step, max(node.index for node in store.list(Node))))
        if event.phase == "finished":
            final_reports.append(json.loads((output / "report.json").read_text()))

    report = run_record(
        package,
        output,
        completion=transport,
        progress=observe,
        options=RunOptions(preload="base", retry_attempts=2),
    )
    assert report["status"] == "recorded" and len(transport.requests) == 3
    assert published == [(0, 0), (1, 1), (2, 2)]
    assert final_reports == [report] and report["verification"]["ok"] and report["cleanup_confirmed"]
    assert [(e.step, e.request, e.attempt) for e in events if e.phase == "model"] == [
        (1, 1, 1),
        (1, 2, 2),
        (2, 3, 1),
    ]
    assert [(e.step, e.attempt) for e in events if e.phase == "retry"] == [(1, 2)]
    assert [(e.step, e.tool, e.tools) for e in events if e.phase == "tool"] == [
        (1, 1, 2),
        (1, 2, 2),
        (2, 1, 1),
    ]
    assert [e.step for e in events if e.phase == "committed"] == [0, 1, 2]
    phases = [e.phase for e in events]
    assert phases[:6] == [
        "preparing",
        "connecting",
        "starting",
        "capturing",
        "committing",
        "committed",
    ]
    assert phases[-3:] == ["cleanup", "verifying", "finished"]
    assert events[-1].status == "recorded" and events[-1].run_end_status == "completed"
    assert "DO-NOT-LOG" not in repr(events) and "command" not in repr(events)


@pytest.mark.parametrize(
    "failure", ["capture", "commit", "incomplete", "cancel", "cleanup", "verify"]
)
def test_progress_does_not_publish_failed_steps_or_premature_success(
    package, tmp_path, runtime, monkeypatch, failure
):
    import palinreplay.record.run as runner

    events = []
    if failure == "capture":
        runtime.capture_error = 1
    elif failure == "commit":
        original = RecordStore.commit_node

        def fail_commit(store, node):
            if node.index == 1:
                raise OSError("injected commit error")
            return original(store, node)

        monkeypatch.setattr(RecordStore, "commit_node", fail_commit)
    elif failure == "cleanup":
        runtime.cleanup_error = True
    elif failure == "verify":

        def fail_verify(*args, **kwargs):
            raise OSError("injected integrity read failure")

        monkeypatch.setattr(runner, "inspect_record", fail_verify)

    reply = (
        response("submit", "write")
        if failure == "incomplete"
        else response("cancel")
        if failure == "cancel"
        else response("submit")
    )
    report = run_record(
        package,
        tmp_path / "run",
        completion=Transport([reply]),
        progress=events.append,
        options=RunOptions(preload="base"),
    )
    committed = [e.step for e in events if e.phase == "committed"]
    assert committed == ([0, 1] if failure in ("cleanup", "verify") else [0])
    assert events[-1].phase == "finished" and events[-1].status == report["status"]
    assert events[-1].status != "recorded"
    assert runtime.sandboxes[0].closed and runtime.clients[0].closed


@pytest.mark.parametrize("debug", [False, True])
def test_progress_observer_failure_never_retries_model(package, tmp_path, runtime, debug):
    calls = []
    transport = Transport([response("submit")])

    def broken(event):
        calls.append(event)
        if event.phase == "model_returned":
            raise RuntimeError("progress rendering failed")

    report = run_record(
        package,
        tmp_path / "run",
        completion=transport,
        progress=broken,
        debug=debug,
        options=RunOptions(preload="base", retry_attempts=3),
    )
    assert report["status"] == "recorded" and report["verification"]["ok"]
    assert len(transport.requests) == 1 and report["storage"]["node_count"] == 2
    assert calls[-1].phase == "model_returned"


@pytest.mark.parametrize("mode", ["auto", "plain", "off"])
@pytest.mark.parametrize("debug", [False, True])
def test_cli_progress_modes_preserve_stdout(
    package, tmp_path, runtime, monkeypatch, capsys, mode, debug
):
    import palinreplay.record.run as runner

    real = runner.run_record
    monkeypatch.setattr(
        runner,
        "run_record",
        lambda *args, **kwargs: real(*args, **kwargs, completion=Transport([response("submit")])),
    )
    assert (
        main(
            [
                "record",
                "run", "--preload", "base",
                str(package),
                "--output",
                str(tmp_path / "run"),
                "--progress",
                mode,
            ] + (["--debug"] if debug else [])
        )
        == 0
    )
    output = capsys.readouterr()
    assert output.out.startswith("Record: recorded ->") and "Report:" in output.out
    assert "[record" not in output.out
    if mode == "off":
        assert "[record" not in output.err
    else:
        assert "Capturing C0" in output.err and "saved and verified" in output.err
        assert "\x1b" not in output.err
        assert ("Step 1 · 2.1" in output.err) == debug
    assert ("Record 流程计时" in output.out) == debug
    timing = json.loads((tmp_path / "run/report.json").read_text())["debug_timing"]
    assert timing["totals"]["model.request"]["count"] == 1
    assert timing["totals"]["tool.execute"]["count"] == 1
    assert [step["index"] for step in timing["flow"]["steps"]] == [0, 1]


def test_cli_default_record_output_uses_task_identity(package, tmp_path, runtime, monkeypatch):
    import palinreplay.cli.outputs as outputs
    import palinreplay.record.run as runner

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(outputs, "timestamp", lambda: "261031_131022")
    real = runner.run_record
    monkeypatch.setattr(runner, "run_record", lambda *args, **kwargs: real(
        *args, **kwargs, completion=Transport([response("submit")]),
    ))
    assert main(["record", "run", str(package), "--preload", "base", "--progress", "off"]) == 0
    task_id = json.loads((package / "manifest.json").read_text())["tasks"][0]["task_id"]
    output = tmp_path / "outputs/record" / f"261031_131022_{task_id}"
    assert json.loads((output / "report.json").read_text())["status"] == "recorded"


def test_cli_sigterm_stops_live_and_restores_cursor(package, tmp_path, runtime, monkeypatch):
    from io import StringIO

    from rich.console import Console

    import palinreplay.cli.progress as ui
    import palinreplay.record.run as runner

    output = StringIO()
    display = RecordProgressDisplay(
        20, console=Console(file=output, force_terminal=True, width=110)
    )
    real = runner.run_record
    live = []

    def completion(**request):
        live.append(display._live)
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(ui, "RecordProgressDisplay", lambda *args: display)
    monkeypatch.setattr(
        runner, "run_record", lambda *a, **kw: real(*a, **kw, completion=completion)
    )
    assert (
        main(
            [
                "record",
                "run",
                "--preload",
                "base",
                str(package),
                "--output",
                str(tmp_path / "run"),
            ]
        )
        == 130
    )
    assert len(live) == 1 and not live[0].is_started
    assert "Record cancelled" in output.getvalue() and "\x1b[?25h" in output.getvalue()
    assert runtime.sandboxes[0].closed and runtime.clients[0].closed
