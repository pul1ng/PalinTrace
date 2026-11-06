from io import StringIO
from types import SimpleNamespace

import pytest

from palinreplay.cli.progress import RecordProgressDisplay
from palinreplay.cli.replay_progress import ReplayProgressDisplay
from palinreplay.cli.resume_progress import ResumeProgressDisplay
from palinreplay.core.progress import ProgressEvent


@pytest.mark.parametrize("kind", ["record", "replay", "resume"])
@pytest.mark.parametrize("stdout_terminal", [False, True])
def test_live_setup_retains_each_flows_output_policy(monkeypatch, kind, stdout_terminal):
    rich = pytest.importorskip("rich.console")
    output = StringIO()
    console = rich.Console(file=output, force_terminal=True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: stdout_terminal)
    factories = {"record": lambda: RecordProgressDisplay(2, console=console),
                 "replay": lambda: ReplayProgressDisplay(console=console),
                 "resume": lambda: ResumeProgressDisplay(2, console=console)}
    with factories[kind]() as display:
        live = display._live if kind == "record" else display._live.live
        assert live._redirect_stdout is (stdout_terminal if kind == "resume" else False)
        assert live._redirect_stderr is (kind == "resume")


@pytest.mark.parametrize("factory", [lambda **kw: RecordProgressDisplay(2, **kw),
                                     ReplayProgressDisplay,
                                     lambda **kw: ResumeProgressDisplay(2, **kw)])
def test_dumb_terminal_keeps_plain_progress(factory):
    output = StringIO()
    console = SimpleNamespace(is_terminal=True, is_dumb_terminal=True)
    with factory(stream=output, console=console) as display:
        display(ProgressEvent("preparing"))
    assert output.getvalue() and "\x1b" not in output.getvalue()


@pytest.mark.parametrize("factory", [lambda **kw: RecordProgressDisplay(2, **kw),
                                     ReplayProgressDisplay,
                                     lambda **kw: ResumeProgressDisplay(2, **kw)])
def test_cancel_after_start_closes_display_before_propagating(factory, monkeypatch):
    rich = pytest.importorskip("rich.console")
    console = rich.Console(file=StringIO(), force_terminal=True)
    display = factory(console=console)
    start = display._start_live
    resources = []

    def cancelled():
        start()
        resources.append(display._live)
        raise KeyboardInterrupt()

    monkeypatch.setattr(display, "_start_live", cancelled)
    with pytest.raises(KeyboardInterrupt), display:
        pytest.fail("cancellation must propagate from context entry")
    assert display._live is None
    assert not getattr(resources[0], "live", resources[0]).is_started


@pytest.mark.parametrize("factory", [lambda **kw: RecordProgressDisplay(2, **kw),
                                     ReplayProgressDisplay,
                                     lambda **kw: ResumeProgressDisplay(2, **kw)])
def test_exit_after_finished_does_not_send_another_finished_event(factory):
    display = factory(mode="plain", stream=StringIO())
    events = []
    update = display._notify.callback

    def observe(event):
        events.append(event)
        update(event)

    display._notify.callback = observe
    with pytest.raises(RuntimeError), display:
        display(ProgressEvent("finished", status="failed"))
        raise RuntimeError("later failure")
    assert [event.phase for event in events] == ["finished"]
