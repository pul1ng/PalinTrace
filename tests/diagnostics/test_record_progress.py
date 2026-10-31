from io import StringIO

import pytest

from palinreplay.cli.progress import RecordProgressDisplay
from palinreplay.core.progress import ProgressEvent, ProgressReporter


@pytest.fixture
def terminal():
    console = pytest.importorskip("rich.console")
    output = StringIO()
    return console.Console(file=output, force_terminal=True, width=110), output


def test_redirected_auto_is_plain_and_only_commits_advance():
    output = StringIO()
    with RecordProgressDisplay(20, stream=output) as progress:
        progress(ProgressEvent("capturing", step=0))
        progress(ProgressEvent("committed", step=0))
        assert progress.committed_steps == 0 and progress.last_node == "C0"
        progress(ProgressEvent("capturing", step=1))
        progress(ProgressEvent("committing", step=1))
        assert progress.committed_steps == 0
        progress(ProgressEvent("committed", step=1))
        progress(ProgressEvent("finished", status="recorded", run_end_status="paused"))
        assert progress.committed_steps == 1 and progress.last_node == "C1"
        assert progress._live is None
    text = output.getvalue()
    assert "Capturing C0" in text and "Committing C1" in text
    assert "paused at Agent budget" in text and "\x1b" not in text


@pytest.mark.parametrize("mode", ["plain", "off"])
def test_explicit_modes_do_not_start_live_even_on_terminal(terminal, mode):
    console, output = terminal
    with RecordProgressDisplay(20, mode, stream=output, console=console) as progress:
        progress(ProgressEvent("model", step=1, request=1, attempt=1))
        progress(ProgressEvent("finished", status="cancelled"))
        assert progress._live is None
    if mode == "off":
        assert output.getvalue() == ""
    else:
        assert "model request 1" in output.getvalue() and "\x1b" not in output.getvalue()


@pytest.mark.parametrize(
    ("status", "run_end_status", "label"),
    [
        ("recorded", "completed", "saved and verified"),
        ("recorded", "paused", "paused at Agent budget"),
        ("incomplete", "incomplete", "unfinished step retained"),
        ("cancelled", "cancelled", "cancelled"),
        ("failed", "failed", "failed"),
    ],
)
def test_live_keeps_real_count_and_stops_for_each_outcome(terminal, status, run_end_status, label):
    console, output = terminal
    with RecordProgressDisplay(20, console=console) as progress:
        live = progress._live
        assert live.is_started and live.auto_refresh and live.refresh_per_second == 4
        progress(ProgressEvent("starting", task_id="test-task"))
        progress(ProgressEvent("committed", step=0))
        progress(ProgressEvent("model", step=1, request=1, attempt=1))
        progress(ProgressEvent("committing", step=1))
        assert progress._steps.tasks[0].completed == 0
        progress(ProgressEvent("committed", step=1))
        progress(ProgressEvent("finished", status=status, run_end_status=run_end_status))
        assert progress._steps.tasks[0].completed == 1
        assert progress._steps.tasks[0].total == 20
        assert progress._activity.tasks[0].stop_time is not None
    assert not live.is_started and progress._live is None
    text = output.getvalue()
    assert "test-task" in text and "Committed steps (budget)" in text
    assert label in text and "C1" in text and "1/20" in text
    assert "100%" not in text and "$0" not in text


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_live_teardown_on_early_failure_or_cancellation(terminal, error):
    console, output = terminal
    with pytest.raises(error), RecordProgressDisplay(20, console=console) as progress:
        live = progress._live
        raise error("private details must not appear")
    assert not live.is_started
    assert ("cancelled" if error is KeyboardInterrupt else "failed") in output.getvalue()
    assert "private details" not in output.getvalue()


def test_render_error_stops_live_without_failing_recording(terminal):
    console, _ = terminal
    with RecordProgressDisplay(20, console=console) as progress:
        live = progress._live

        def broken(event):
            raise OSError("broken terminal")

        progress._notify.callback = broken
        progress(ProgressEvent("committed", step=1))
        assert not live.is_started
        progress(ProgressEvent("finished", status="recorded"))


def test_live_setup_error_falls_back_to_plain(terminal, monkeypatch):
    console, output = terminal
    progress = RecordProgressDisplay(20, stream=output, console=console)

    def broken():
        raise OSError("terminal setup error")

    monkeypatch.setattr(progress, "_start_live", broken)
    with progress:
        progress(ProgressEvent("capturing", step=0))
    assert "Capturing C0" in output.getvalue() and "\x1b" not in output.getvalue()


def test_cancel_during_live_start_still_restores_terminal(terminal, monkeypatch):
    console, output = terminal
    progress = RecordProgressDisplay(20, console=console)
    start = progress._start_live
    lives = []

    def cancelled():
        start()
        lives.append(progress._live)
        raise KeyboardInterrupt()

    monkeypatch.setattr(progress, "_start_live", cancelled)
    with pytest.raises(KeyboardInterrupt), progress:
        pytest.fail("cancelled during __enter__")
    assert not lives[0].is_started and "\x1b[?25h" in output.getvalue()


def test_observer_errors_are_disabled_but_interrupts_propagate():
    calls = []

    def broken(event):
        calls.append(event)
        raise RuntimeError("observer failure")

    reporter = ProgressReporter(broken)
    reporter(ProgressEvent("preparing"))
    reporter(ProgressEvent("starting"))
    assert len(calls) == 1

    def cancelled(event):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        ProgressReporter(cancelled)(ProgressEvent("model"))


def test_plain_live_flow_has_step_numbers_durations_and_no_false_commits():
    from palinreplay.diagnostics.record_timing import (
        flow_scope,
        record_phase,
        record_step,
        record_timing,
    )

    output = StringIO()
    with RecordProgressDisplay(2, "plain", stream=output) as progress:
        with record_timing(True, progress=progress):
            with flow_scope("1.1"):
                assert "1.1 检查运行参数: 开始" in output.getvalue()
            with record_step(0), flow_scope("1.6"):
                with flow_scope("S1"):
                    pass
            record_phase("steps")
            for index in (1, 2):
                with record_step(index), flow_scope("2.1"):
                    with flow_scope("2.2"):
                        pass
            with pytest.raises(OSError), record_step(2), flow_scope("2.7"):
                raise OSError("PRIVATE-CAPTURE")
            record_phase("finalization")
            with flow_scope("3.2"):
                pass
        assert progress.committed_steps == 0 and progress.last_node == "none"
    text = output.getvalue()
    for expected in (
        "阶段 1 开始初始化", "阶段 2 Step 循环", "阶段 3 最终收尾",
        "C0 · 1.6", "C0 · S1", "Step 1 · 2.1", "Step 2 · 2.1", "3.2 清理运行资源",
        "本段耗时", "含子项", "Step 2 · 2.7 捕获状态并提交 Ci: 结束 (控制流/异常)",
    ):
        assert expected in text
    assert "Committed" not in text and "PRIVATE" not in text and "\x1b" not in text


def test_live_flow_is_kept_in_terminal_history_without_advancing_budget(terminal):
    console, output = terminal
    with RecordProgressDisplay(2, console=console) as progress:
        progress(ProgressEvent("flow_start", code="2.1", label="检查预算", step=1))
        assert "Step 1 · 2.1" in output.getvalue()
        progress(ProgressEvent(
            "flow_end", code="2.1", label="检查预算", step=1, status="finished",
            duration_sec=0.25, inclusive_sec=1.0,
        ))
        # The following step must not erase the completed line from history.
        progress(ProgressEvent("flow_start", code="2.1", label="检查预算", step=2))
        text = output.getvalue()
        assert "0.250000s" in text and "1.000000s" in text and "Step 2 · 2.1" in text
        assert progress._steps.tasks[0].completed == 0


def test_progress_off_hides_live_flow_without_losing_measurements():
    from palinreplay.diagnostics.record_timing import flow_scope, record_timing

    output = StringIO()
    with RecordProgressDisplay(2, "off", stream=output) as progress:
        with record_timing(True, progress=progress) as timing:
            with flow_scope("1.1"):
                pass
    assert output.getvalue() == "" and timing.totals["1.1"]["count"] == 1


def test_flow_subitems_have_distinct_names_indentation_and_tool_ordinals():
    from palinreplay.diagnostics.record_timing import flow_scope, record_step, record_timing

    output = StringIO()
    with RecordProgressDisplay(2, "plain", stream=output) as progress:
        with record_timing(True, progress=progress):
            for part in ("1.2.1", "1.2.2", "1.2.3"):
                with flow_scope("1.2", part=part):
                    pass
            with record_step(1), flow_scope("2.1", part="2.1.1"):
                with flow_scope("2.1", part="2.1.2"):
                    pass
            with record_step(1), flow_scope("2.5", part="2.5.1"):
                for tool in (1, 2, 3):
                    with flow_scope("2.4", tool=tool, tools=3):
                        pass
    text = output.getvalue()
    for part in ("1.2.1 读取任务包", "1.2.2 检查并加载 Agent", "1.2.3 读取模型连接配置"):
        assert text.count(f"{part}: 开始") == 1
    assert "1.2 加载任务与模型配置: 开始" not in text
    assert "]   Step 1 · 2.1.2 创建本步录制记录: 开始" in text
    for tool in (1, 2, 3):
        assert f"Step 1 · 2.4 执行工具并录制 · 工具 {tool}/3: 开始" in text
