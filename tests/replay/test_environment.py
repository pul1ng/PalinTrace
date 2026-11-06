import threading
from types import SimpleNamespace

import pytest

from palinreplay.core.errors import IntegrityError
from palinreplay.restoration.environment import RestoredExecutor
from palinreplay.sandbox.base import SandboxTimeout


def environment(monkeypatch, execute):
    calls, cleanups = [], []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return execute(command, kwargs)

    container = SimpleNamespace(exec_run=run)
    session = SimpleNamespace(
        pool=object(), container=container,
        workspace=SimpleNamespace(name="workspaces/fixture", container_name="restored"),
        close=lambda: cleanups.append("closed"),
    )
    runtime = SimpleNamespace(launch=SimpleNamespace(
        workdir="/testbed", shell=("bash", "-c"), environment={"BASE": "saved"},
    ))
    monkeypatch.setattr(RestoredExecutor, "check_root", lambda self: None)
    return RestoredExecutor(session, runtime), calls, cleanups


def test_exact_request_and_borrowed_lifetime(monkeypatch):
    env, calls, cleanups = environment(
        monkeypatch, lambda command, kwargs: SimpleNamespace(exit_code=7, output=b"a\xff\r\n"),
    )
    req = {"action": {"command": "printf test"}, "cwd": "/other", "env": {"X": "value"},
           "interpreter": ["bash", "-c"], "timeout_sec": 3}
    result = env.execute(req)
    assert result.output == b"a\xff\r\n" and result.exit_code == 7
    command, options = calls[0]
    assert command == ["bash", "-c", "printf test"]
    assert options["environment"] == {"BASE": "saved", "X": "value"}
    assert options["workdir"] == "/other" and not options["tty"]
    with pytest.raises(IntegrityError):
        env.execute(req | {"interpreter": ["sh", "-c"]})
    env.close()
    assert len(calls) == 1 and cleanups == []


def test_timeout_delegates_cleanup_once_without_retry(monkeypatch):
    released = threading.Event()

    def execute(*args):
        released.wait(1)
        return SimpleNamespace(exit_code=0, output=b"late")

    env, calls, cleanups = environment(monkeypatch, execute)
    try:
        with pytest.raises(SandboxTimeout):
            env.execute({"action": {"command": "slow"}, "cwd": "/testbed", "env": {},
                         "interpreter": ["bash", "-c"], "timeout_sec": 0.01})
        assert len(calls) == 1 and cleanups == ["closed"] and env.state == "failed"
    finally:
        released.set()


def test_saved_execution_defaults_and_explicit_overrides(monkeypatch):
    env, calls, cleanups = environment(
        monkeypatch, lambda command, kwargs: SimpleNamespace(exit_code=0, output=b"ok"),
    )
    saved = {"cwd": "/saved", "interpreter": ["bash", "-c"], "timeout": 17,
             "env": {"PAGER": "cat", "TZ": "Etc/UTC"}}
    env = RestoredExecutor(env.restored, env.runtime, execution_config=saved)
    assert env._timeout == 17
    env.exec("pwd")
    assert calls[-1][0] == ["bash", "-c", "pwd"]
    assert calls[-1][1]["workdir"] == "/saved"
    assert calls[-1][1]["environment"] == {"BASE": "saved", "PAGER": "cat", "TZ": "Etc/UTC"}
    env.exec("pwd", cwd="/override", env={"TZ": "override"}, timeout_sec=3)
    assert calls[-1][1]["workdir"] == "/override"
    assert calls[-1][1]["environment"]["TZ"] == "override"
    assert saved["env"]["TZ"] == "Etc/UTC"
    env.close()
    assert cleanups == []
