from io import StringIO

import pytest

from palinreplay.cli.resume_progress import ResumeProgressDisplay
from palinreplay.core.progress import ProgressEvent


def test_plain_progress_counts_new_commits_and_keeps_the_source_as_a_reference():
    output = StringIO()
    with ResumeProgressDisplay(100, stream=output) as progress:
        progress(ProgressEvent("restoring", source_step=20))
        progress(ProgressEvent("verifying", source_step=20))
        progress(ProgressEvent("service-preparation"))
        progress(ProgressEvent("source-reference", source_step=20, completed_steps=0))
        for event in (
            ProgressEvent("model", step=21, request=1),
            ProgressEvent("model_returned", step=21),
            ProgressEvent("tool", step=21, tool=1, tools=2),
            ProgressEvent("tool_returned", step=21, tool=1, tools=2),
            ProgressEvent("capturing", step=21),
            ProgressEvent("committing", step=21),
        ):
            progress(event)
            assert progress.completed == 0 and progress.last_node == "源 C20 (引用)"
        progress(ProgressEvent("committed", step=21, completed_steps=1))
        progress(ProgressEvent("cleanup"))
        progress(ProgressEvent("finished", status="submitted", completed_steps=1, step=21))
        assert progress.completed == 1 and progress.last_node == "C21"
    text = output.getvalue()
    assert "0/100" in text and "1/100" in text and "21/100" not in text
    assert "模型已提交" in text and "清理临时资源" in text and "工具 1/2" in text
    assert "\x1b" not in text


@pytest.mark.parametrize("mode", ["auto", "plain", "off"])
@pytest.mark.parametrize("status", ["submitted", "budget_exhausted", "failed", "cancelled", "incomplete"])
def test_terminal_ends_at_real_progress_without_filling_the_budget(mode, status):
    rich = pytest.importorskip("rich.console")
    output = StringIO()
    console = rich.Console(file=output, force_terminal=True, width=130)
    with ResumeProgressDisplay(100, mode, stream=output, console=console) as progress:
        display = progress._live
        progress(ProgressEvent("restoring", source_step=20))
        progress(ProgressEvent("committed", step=34, completed_steps=14))
        progress(ProgressEvent("finished", status=status, completed_steps=14, step=34))
        assert progress.completed == 14 and progress.last_node == "C34"
        if mode == "auto":
            assert display.tasks[0].completed == 14 and display.tasks[0].total == 100
            assert display.tasks[0].stop_time is not None
            assert not display.live.is_started
        else:
            assert display is None
    if mode == "off":
        assert not output.getvalue()
    else:
        text = output.getvalue()
        assert "14/100" in text and "C34" in text and "已保存/预算" in text
        assert "100%" not in text
        if mode == "plain":
            assert "\x1b" not in text


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_early_failure_restores_the_terminal(error):
    rich = pytest.importorskip("rich.console")
    output = StringIO()
    console = rich.Console(file=output, force_terminal=True, width=130)
    with pytest.raises(error), ResumeProgressDisplay(100, console=console) as progress:
        display = progress._live
        raise error("private details")
    assert not display.live.is_started and "\x1b[?25h" in output.getvalue()
    assert "private details" not in output.getvalue()


def test_live_setup_failure_uses_plain_output(monkeypatch):
    output = StringIO()
    progress = ResumeProgressDisplay(2, stream=output)
    monkeypatch.setattr(output, "isatty", lambda: True)

    def unavailable():
        raise ImportError("Rich unavailable")

    monkeypatch.setattr(progress, "_start_live", unavailable)
    with progress:
        progress(ProgressEvent("preparing"))
    assert "0/2" in output.getvalue() and "\x1b" not in output.getvalue()


def test_render_failure_stops_display_without_aborting_execution():
    rich = pytest.importorskip("rich.console")
    output = StringIO()
    console = rich.Console(file=output, force_terminal=True, width=130)
    with ResumeProgressDisplay(2, console=console) as progress:
        display = progress._live

        def broken(event):
            raise OSError("broken terminal")

        progress._notify.callback = broken
        progress(ProgressEvent("model", step=21, request=1))
        assert not display.live.is_started
        progress(ProgressEvent("finished", status="submitted"))
