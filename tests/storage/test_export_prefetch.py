import _socket
import hashlib
import socket
import tarfile
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import pytest

import palinreplay.storage.export_stream as _export_prefetch
import palinreplay.storage.rootfs as _rootfs
from palinreplay.diagnostics.metrics import measure
from palinreplay.sandbox.base import SandboxCleanupError
from palinreplay.storage.export_stream import CHUNK_BYTES, QUEUE_SLOTS, prefetch_export
from tests.storage.test_rootfs_manifest import SOURCE, files, tar_bytes
from tests.storage.test_rootfs_stream import Export


class Source:
    def __init__(self, export):
        self.export = export
        self.cancelled = False

    def chunks(self):
        return self.export.export(CHUNK_BYTES)

    def cancel(self):
        self.cancelled = True


@pytest.mark.parametrize("chunk_size", [1, 511, 65536, CHUNK_BYTES])
def test_prefetch_keeps_tar_manifest_offsets_digest_and_io(tmp_path, monkeypatch, chunk_size):
    raw = tar_bytes([*files(), ("empty", tarfile.REGTYPE, b"", 0o640),
                     ("long/" + "x" * 180 + "/中文", tarfile.REGTYPE, b"pax", 0o600)])
    original = tmp_path / "original"
    original.write_bytes(raw)
    logical, positions = _rootfs.inventory(original)
    source = Source(Export(raw, chunk=chunk_size))
    monkeypatch.setattr(_export_prefetch, "DockerExport", lambda _: source)
    with measure() as io_metrics:
        result = _rootfs.export_inventory(None, tmp_path / "staged", prefetch=True)
    assert (result.logical, result.positions) == (logical, positions)
    assert result.sha256 == hashlib.sha256(raw).hexdigest()
    assert (tmp_path / "staged").read_bytes() == raw
    assert source.export.closed and source.export.eof and not source.cancelled
    assert io_metrics.counters["written_bytes"] == io_metrics.counters["exported_bytes"] == len(raw)
    assert io_metrics.counters["read_bytes"] == 0
    stats = result.transport
    assert stats["joined"] and stats["chunks"]["total_bytes"] == len(raw)
    assert stats["chunks"]["max_bytes"] <= chunk_size
    assert stats["queue_peak_slots"] <= QUEUE_SLOTS
    assert stats["queue_peak_bytes"] <= CHUNK_BYTES * QUEUE_SLOTS
    assert stats["consumer_first_byte_wait_sec"] >= 0


def test_full_queue_is_bounded_and_early_consumer_failure_joins(monkeypatch):
    fifth, closed = threading.Event(), threading.Event()
    class Queued:
        def chunks(self):
            try:
                for index in range(100):
                    if index == QUEUE_SLOTS:
                        fifth.set()
                    yield bytes(CHUNK_BYTES)
            finally:
                closed.set()
        def cancel(self):
            pass
    monkeypatch.setattr(_export_prefetch, "DockerExport", lambda _: Queued())
    with pytest.raises(OSError, match="consumer failed"):
        with prefetch_export(None) as stream:
            assert fifth.wait(5)
            assert stream.queue_peak_slots == QUEUE_SLOTS
            assert stream.queue_peak_bytes == QUEUE_SLOTS * CHUNK_BYTES
            raise OSError("consumer failed")
    assert closed.is_set()
    assert not any(t.name == "palinreplay-export" for t in threading.enumerate())


@pytest.mark.parametrize("case", ["start", "late-read", "close", "oversized", "view", "type"])
def test_producer_failures_are_collected_even_after_consumer_eof(monkeypatch, case):
    source = Source(Export(b"data", fail=case == "late-read", close_fail=case == "close"))
    if case == "start":
        def broken():
            raise OSError("source initialization failed")
        source.chunks = broken
    elif case in {"oversized", "view", "type"}:
        data = {"oversized": bytes(CHUNK_BYTES + 1),
                "view": memoryview(bytearray(CHUNK_BYTES + 1)).cast("B", (1, CHUNK_BYTES + 1)),
                "type": "invalid"}[case]
        source.chunks = lambda: iter([data])
    monkeypatch.setattr(_export_prefetch, "DockerExport", lambda _: source)
    with pytest.raises((OSError, ValueError)):
        with prefetch_export(None) as stream:
            list(stream)
    assert not any(t.name == "palinreplay-export" for t in threading.enumerate())


def test_normal_early_abandonment_also_cancels(monkeypatch):
    source = Source(Export(b"data"))
    monkeypatch.setattr(_export_prefetch, "DockerExport", lambda _: source)
    with pytest.raises(ValueError, match="before EOF"):
        with prefetch_export(None):
            pass
    assert source.cancelled


def test_mutable_source_blocks_are_frozen_before_queueing(monkeypatch):
    buffer = bytearray(b"one")
    class Mutable:
        def chunks(self):
            yield buffer
            buffer[:] = b"two"
            yield buffer
        def cancel(self):
            pass
    monkeypatch.setattr(_export_prefetch, "DockerExport", lambda _: Mutable())
    with prefetch_export(None) as stream:
        assert list(stream) == [b"one", b"two"]


@contextmanager
def unix_docker_server(mode, raw):
    """Actual Docker SDK HTTP parsing and sockets; no Docker daemon required."""
    ready, disconnected = threading.Event(), threading.Event()
    failures = []
    with tempfile.TemporaryDirectory(prefix="pr-prefetch-", dir="/tmp") as directory:
        path = str(Path(directory) / "docker.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)
        listener.settimeout(5)
        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(5)
                    request = bytearray()
                    while not request.endswith(b"\r\n\r\n"):
                        request.extend(connection.recv(1))
                    assert request.startswith(f"GET /v1.52/containers/{SOURCE}/export ".encode())
                    if mode != "headers":
                        connection.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
                        connection.sendall(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
                        if mode == "complete":
                            connection.sendall(b"0\r\n\r\n")
                        elif mode == "bad-trailer":
                            connection.sendall(b"not-a-chunk\r\n")
                    ready.set()
                    assert connection.recv(1) == b""
                    disconnected.set()
            except BaseException as error:
                failures.append(error)
            finally:
                listener.close()
        server = threading.Thread(target=serve, name="fixture-docker-http", daemon=True)
        server.start()
        api = SimpleNamespace(_custom_adapter=SimpleNamespace(socket_path=path),
                              base_url="http+docker://localhost", _version="1.52", timeout=5)
        container = SimpleNamespace(id=SOURCE, client=SimpleNamespace(api=api))
        def local_only(sock, address):
            # Keep the suite's network ban; permit just this disposable Unix
            # socket to exercise the real SDK's framing and cancellation path.
            assert sock.family == socket.AF_UNIX and address == path
            return _socket.socket.connect(sock, address)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(socket.socket, "connect", local_only)
            try:
                yield container, ready, disconnected
            finally:
                server.join(6)
                assert not server.is_alive() and not failures, failures


@pytest.mark.parametrize("mode", ["headers", "body"])
def test_owned_sdk_socket_is_cancelled_before_headers_or_during_body(mode):
    with unix_docker_server(mode, b"part") as (container, ready, disconnected):
        started = monotonic()
        with pytest.raises(KeyboardInterrupt):
            with prefetch_export(container) as stream:
                assert ready.wait(3)
                if mode == "body":
                    assert next(stream) == b"part"
                raise KeyboardInterrupt("cancel blocked HTTP read")
        assert monotonic() - started < 3
        assert disconnected.wait(1)


@pytest.mark.parametrize("mode", ["complete", "bad-trailer"])
def test_sdk_transport_errors_after_tar_eof_are_not_swallowed(tmp_path, mode):
    from requests.exceptions import ChunkedEncodingError

    raw = tar_bytes(files())
    with unix_docker_server(mode, raw) as (container, _ready, disconnected):
        if mode == "complete":
            result = _rootfs.export_inventory(container, tmp_path / "staged", prefetch=True)
            assert result.sha256 == hashlib.sha256(raw).hexdigest()
        else:
            with pytest.raises(ChunkedEncodingError):
                _rootfs.export_inventory(container, tmp_path / "staged", prefetch=True)
        assert disconnected.wait(1)


def test_owned_sdk_cleanup_failure_is_fatal(tmp_path, monkeypatch):
    import docker

    close = docker.APIClient.close
    def failed(api):
        close(api)
        raise OSError("injected private pool close failure")
    monkeypatch.setattr(docker.APIClient, "close", failed)
    with unix_docker_server("complete", tar_bytes(files())) as (container, _ready, disconnected):
        with pytest.raises(SandboxCleanupError, match="owned export resources"):
            _rootfs.export_inventory(container, tmp_path / "staged", prefetch=True)
        assert disconnected.wait(1)


@pytest.mark.parametrize("members", [
    [("../escape", tarfile.REGTYPE, b"x", 0o640)],
    [(".wh.hidden", tarfile.REGTYPE, b"x", 0o640)],
    [("a", tarfile.LNKTYPE, "b", 0o640), ("b", tarfile.LNKTYPE, "a", 0o640)],
    [("a", tarfile.REGTYPE, b"x", 0o640), ("a", tarfile.REGTYPE, b"y", 0o640)],
])
def test_prefetch_preserves_path_and_link_validation(tmp_path, monkeypatch, members):
    source = Source(Export(tar_bytes(members)))
    monkeypatch.setattr(_export_prefetch, "DockerExport", lambda _: source)
    with pytest.raises(ValueError):
        _rootfs.export_inventory(None, tmp_path / "staged", prefetch=True)
    assert source.export.closed and not (tmp_path / "escape").exists()


def test_distribution_is_bounded_and_large_view_is_not_counted_as_one_byte():
    stats = _export_prefetch.ChunkStats()
    for size in range(200):
        stats.add(bytes(size))
    report = stats.snapshot()
    assert len(report["sizes"]) == 128 and report["other_sizes_count"] == 72
    assert report["total_bytes"] == sum(range(200))
    assert report["min_bytes"] == 0 and report["max_bytes"] == 199


def test_serial_path_retains_chunk_diagnostics(tmp_path):
    raw = tar_bytes(files())
    result = _rootfs.export_inventory(Export(raw, chunk=511), tmp_path / "serial")
    assert result.transport["enabled"] is False
    assert result.transport["chunks"]["total_bytes"] == len(raw)
    assert result.transport["chunks"]["max_bytes"] == 511
    assert result.transport["consumer_first_byte_wait_sec"] >= 0


def test_prefetch_requires_boolean_option(tmp_path):
    with pytest.raises(ValueError, match="boolean"):
        _rootfs.export_inventory(None, tmp_path / "invalid", prefetch=1)
