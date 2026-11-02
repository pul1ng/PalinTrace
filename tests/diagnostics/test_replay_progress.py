from io import StringIO

import pytest

from palinreplay.cli.replay_progress import ReplayProgressDisplay
from palinreplay.core.progress import ProgressEvent


def test_redirected_progress_is_flushed_and_does_not_advance_during_work():
    output = StringIO()
    with ReplayProgressDisplay(stream=output) as progress:
        progress(ProgressEvent("preparing", total_steps=43))
        progress(ProgressEvent("restoring", source_step=1, step=0))
        progress(ProgressEvent("committed", source_step=1, step=0))
        progress(ProgressEvent("replaying", source_step=2, step=1))
        progress(ProgressEvent("verifying", source_step=2, step=1))
        assert progress.completed == 0
        assert "0/43" in output.getvalue() and "校验 源 C2 → C1" in output.getvalue()
        progress(ProgressEvent("committed", source_step=2, step=1))
        progress(ProgressEvent("cleanup"))
        progress(ProgressEvent("finished", status="stopped", label="STOPPED / DIFFERENCE"))
        assert progress.completed == 1
    text = output.getvalue()
    assert "1/43" in text and "STOPPED / DIFFERENCE" in text
    assert "43/43" not in text and "\x1b" not in text


@pytest.mark.parametrize("mode", ["auto", "plain", "off"])
@pytest.mark.parametrize("status,completed", [("completed", 3), ("stopped", 1), ("failed", 1)])
def test_terminal_keeps_actual_progress_and_validation_separate(mode, status, completed):
    rich = pytest.importorskip("rich.console")
    output = StringIO()
    console = rich.Console(file=output, force_terminal=True, width=110)
    with ReplayProgressDisplay(mode, stream=output, console=console) as progress:
        display = progress._progress
        progress(ProgressEvent("preparing", total_steps=3))
        progress(ProgressEvent("committed", source_step=20 + completed, step=completed))
        label = f"{status.upper()} / DIFFERENCE"
        progress(ProgressEvent("finished", status=status, label=label))
        if mode == "auto":
            assert display.live.is_started
            assert display.tasks[0].completed == completed and display.tasks[0].total == 3
            assert display.tasks[0].stop_time is not None
        else:
            assert display is None
    if display:
        assert not display.live.is_started
    if mode == "off":
        assert output.getvalue() == ""
    else:
        text = output.getvalue()
        assert label in text and f"{completed}/3" in text and "MATCH" not in text
        if mode == "plain":
            assert "\x1b" not in text


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_terminal_is_restored_after_error_or_cancellation(error):
    rich = pytest.importorskip("rich.console")
    output = StringIO()
    console = rich.Console(file=output, force_terminal=True, width=110)
    with pytest.raises(error), ReplayProgressDisplay(console=console) as progress:
        display = progress._progress
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
