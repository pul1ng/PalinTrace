import pytest

import palinreplay.preload.build as preload_build
from palinreplay.sandbox.base import SandboxCleanupError
from palinreplay.storage.store import IntegrityError
from tests.preload.test_preload import build, plan
from tests.preload.test_preload import runtime as runtime
from tests.preload.test_preload_environment import inputs


def install_pool_cleanup_failures(monkeypatch, *, close_error, discard_error):
    original_create = preload_build.BtrfsPool.create
    calls = []

    def create(client):
        pool = original_create(client)
        original_close = pool.close

        def close():
            calls.append("close")
            original_close()
            raise close_error

        def discard():
            calls.append("discard")
            raise discard_error

        pool.close, pool.discard = close, discard
        return pool

    monkeypatch.setattr(preload_build.BtrfsPool, "create", create)
    return calls


def test_import_error_survives_close_discard_and_client_errors(tmp_path, runtime, monkeypatch):
    primary = IntegrityError("source and imported files differ")

    def fail_import(*args):
        raise primary

    monkeypatch.setattr(preload_build, "_import_c0", fail_import)
    calls = install_pool_cleanup_failures(
        monkeypatch, close_error=SandboxCleanupError("release unconfirmed"),
        discard_error=RuntimeError("secondary discard failure"),
    )
    client = runtime[0]
    monkeypatch.setattr(preload_build, "connect_docker", lambda timeout: client)

    def fail_client_close():
        client.closed = True
        raise OSError("secondary client failure")

    monkeypatch.setattr(client, "close", fail_client_close)
    output = tmp_path / "base"
    with pytest.raises(IntegrityError) as raised:
        preload_build.build_preload(
            inputs(), output, initialization=plan(), public_env_keys=("LANG", "PATH"),
        )
    assert raised.value is primary
    assert calls == ["close", "discard"]
    assert len(primary.__notes__) == 3
    assert all(word in " ".join(primary.__notes__) for word in ("close", "discard", "client"))
    assert client.closed and all(c.removed for c in client.created)
    assert not output.exists()


def test_first_release_error_is_not_replaced_by_discard_error(tmp_path, runtime, monkeypatch):
    primary = SandboxCleanupError("first loop device release unconfirmed")
    install_pool_cleanup_failures(
        monkeypatch, close_error=primary, discard_error=RuntimeError("helper already removed"),
    )
    output = tmp_path / "base"
    with pytest.raises(SandboxCleanupError) as raised:
        build(inputs(), output, runtime)
    assert raised.value is primary
    assert "discard also failed" in primary.__notes__[0]
    assert not output.exists() and all(c.removed for c in runtime[0].created)


def test_cleanup_fault_still_takes_precedence_over_cancellation(tmp_path, runtime, monkeypatch):
    primary = SandboxCleanupError("release failed after cancellation")

    def cancel(*args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(preload_build, "_import_c0", cancel)
    install_pool_cleanup_failures(
        monkeypatch, close_error=primary, discard_error=RuntimeError("secondary cleanup"),
    )
    output = tmp_path / "base"
    with pytest.raises(SandboxCleanupError) as raised:
        build(inputs(), output, runtime)
    assert raised.value is primary and isinstance(primary.__context__, KeyboardInterrupt)
    assert not output.exists()


def test_discard_fault_during_cancel_is_reported(tmp_path, runtime, monkeypatch):
    original_create = preload_build.BtrfsPool.create
    cleanup = SandboxCleanupError("cancel cleanup could not finish")

    def create(client):
        pool = original_create(client)

        def cancel_enter():
            raise KeyboardInterrupt()

        def fail_discard():
            raise cleanup

        monkeypatch.setattr(type(pool), "__enter__", lambda self: cancel_enter())
        pool.discard = fail_discard
        return pool

    monkeypatch.setattr(preload_build.BtrfsPool, "create", create)
    output = tmp_path / "base"
    with pytest.raises(SandboxCleanupError) as raised:
        build(inputs(), output, runtime)
    assert raised.value is cleanup and isinstance(cleanup.__context__, KeyboardInterrupt)
    assert not output.exists()
