from io import StringIO

import pytest

from palinreplay.cli.replay_progress import ReplayProgressDisplay
from palinreplay.core.progress import ProgressEvent


def test_redirected_progress_is_flushed_and_does_not_advance_during_work():
    output = StringIO()
    with ReplayProgressDisplay(stream=output) as progress:
        progress(ProgressEvent("preparing", total_steps=20))
        progress(ProgressEvent("restoring", source_step=20, completed_steps=0))
        progress(ProgressEvent("service-preparation"))
        assert progress.completed == 0 and "准备指定服务" in output.getvalue()
        progress(ProgressEvent("source-reference", source_step=20, completed_steps=0))
        progress(ProgressEvent("replaying", source_step=21, step=21, completed_steps=0))
        assert progress.completed == 0 and "源 C20 (引用)" in output.getvalue()
        assert "0/20" in output.getvalue() and "源 C21 → C21" in output.getvalue()
        progress(ProgressEvent("committed", source_step=21, step=21, completed_steps=1))
        progress(ProgressEvent("cleanup"))
        progress(ProgressEvent("finished", status="stopped", label="STOPPED"))
        assert progress.completed == 1
    text = output.getvalue()
    assert "1/20" in text and "STOPPED" in text
    assert "20/20" not in text and "\x1b" not in text


@pytest.mark.parametrize("mode", ["auto", "plain", "off"])
@pytest.mark.parametrize("status,completed", [("completed", 20), ("stopped", 1), ("failed", 1), ("completed", 0)])
def test_terminal_keeps_actual_progress_separate_from_node_number(mode, status, completed):
    rich = pytest.importorskip("rich.console")
    output = StringIO()
    console = rich.Console(file=output, force_terminal=True, width=110)
    with ReplayProgressDisplay(mode, stream=output, console=console) as progress:
        display = progress._live
        total = 0 if completed == 0 else 20
        progress(ProgressEvent("preparing", total_steps=total))
        progress(ProgressEvent("source-reference", source_step=20, completed_steps=0))
        if completed:
            progress(ProgressEvent("committed", source_step=20 + completed, step=20 + completed,
                                   completed_steps=completed))
        label = status.upper()
        progress(ProgressEvent("finished", status=status, label=label))
        assert progress.completed == completed
        if mode == "auto":
            assert display.live.is_started
            assert display.tasks[0].completed == completed and display.tasks[0].total == total
            assert display.tasks[0].stop_time is not None
        else:
            assert display is None
    if display:
        assert not display.live.is_started
    if mode == "off":
        assert output.getvalue() == ""
    else:
        text = output.getvalue()
        assert label in text and f"{completed}/{total}" in text and "MATCH" not in text
        if mode == "plain":
            assert "\x1b" not in text


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_terminal_is_restored_after_error_or_cancellation(error):
    rich = pytest.importorskip("rich.console")
    output = StringIO()
    console = rich.Console(file=output, force_terminal=True, width=110)
    with pytest.raises(error), ReplayProgressDisplay(console=console) as progress:
        display = progress._live
        progress(ProgressEvent("preparing", total_steps=43))
        raise error("private error details")
    assert not display.live.is_started and "\x1b[?25h" in output.getvalue()
    assert ("CANCELLED" if error is KeyboardInterrupt else "FAILED") in output.getvalue()
    assert "private error details" not in output.getvalue()


def test_live_setup_failure_falls_back_to_plain(monkeypatch):
    output = StringIO()
    progress = ReplayProgressDisplay(stream=output)
    monkeypatch.setattr(output, "isatty", lambda: True)

    def unavailable():
        raise ImportError("Rich unavailable")

    monkeypatch.setattr(progress, "_start_live", unavailable)
    with progress:
        progress(ProgressEvent("preparing", total_steps=2))
    assert "0/2" in output.getvalue() and "\x1b" not in output.getvalue()
