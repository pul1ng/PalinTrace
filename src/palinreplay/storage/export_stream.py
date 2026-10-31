"""Bounded Docker export prefetch, with a privately owned cancellable connection."""

import errno
import socket
import threading
from collections import deque
from contextlib import contextmanager
from time import monotonic

from palinreplay.sandbox.base import SandboxCleanupError
from palinreplay.storage.capture_worker import capture_worker

CHUNK_BYTES = 1024 * 1024
QUEUE_SLOTS = 4


class ChunkStats:
    """Bounded size distribution; data blocks, not TCP packets or physical IO."""

    def __init__(self):
        self.count = self.total = self.maximum = 0
        self.minimum = None
        self.sizes = {}
        self.other = 0

    def add(self, chunk):
        size = len(chunk)
        self.count += 1
        self.total += size
        self.minimum = size if self.minimum is None else min(self.minimum, size)
        self.maximum = max(self.maximum, size)
        if size in self.sizes or len(self.sizes) < 128:
            self.sizes[size] = self.sizes.get(size, 0) + 1
        else:
            self.other += 1

    def snapshot(self):
        return {"count": self.count, "total_bytes": self.total,
                "min_bytes": self.minimum, "max_bytes": self.maximum,
                "sizes": {str(k): v for k, v in sorted(self.sizes.items())},
                "other_sizes_count": self.other}


class DockerExport:
    """Keep Docker SDK framing, but never cancel a shared pooled connection.

    This experiment supports the same local Unix transport as sandbox/client.py.
    The dedicated SDK connection's connect hook registers its socket before HTTP
    headers are read, so cancellation can interrupt a blocked response too. The
    pool is private to one GET and is closed by the producer, never reused by the
    coordinator or a concurrent census helper.
    """

    def __init__(self, container):
        api = container.client.api
        adapter = getattr(api, "_custom_adapter", None)
        path = getattr(adapter, "socket_path", None)
        if (api.base_url != "http+docker://localhost" or not isinstance(path, str)
                or not path.startswith("/") or "\x00" in path):
            raise ValueError("export prefetch requires the configured local Unix Docker socket")
        self.path, self.version, self.timeout = path, api._version, api.timeout
        self.container_id = container.id
        self.lock = threading.Lock()
        self.socket = None
        self.stopped = threading.Event()

    def cancel(self):
        self.stopped.set()
        with self.lock:
            if self.socket is not None:
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except OSError as error:
                    if error.errno not in {errno.ENOTCONN, errno.EBADF}:
                        raise SandboxCleanupError("cannot interrupt owned export connection") from error

    def chunks(self):
        import docker

        api = response = stream = None
        primary = None
        try:
            if self.stopped.is_set():
                return
            api = docker.APIClient(base_url="unix://" + self.path, version=self.version,
                                   timeout=self.timeout, num_pools=1, max_pool_size=1)
            url = api._url("/containers/{0}/export", self.container_id)
            pool = api._custom_adapter.get_connection(url)
            make_connection = pool._new_conn

            def connection():
                value = make_connection()
                connect = value.connect
                def connected():
                    connect()
                    with self.lock:
                        self.socket = value.sock
                    if self.stopped.is_set():
                        self.cancel()
                value.connect = connected
                return value

            pool._new_conn = connection
            response = api._get(url, stream=True)
            stream = api._stream_raw_result(response, CHUNK_BYTES, False)
            yield from stream
        except BaseException as error:
            primary = error
            raise
        finally:
            # Detached under the same lock as cancellation; cleanup cannot hand
            # a live connection to another API user because this pool is private.
            with self.lock:
                self.socket = None
            errors = []
            for resource in (stream, response, api):
                if resource is not None:
                    try:
                        resource.close()
                    except BaseException as error:
                        errors.append(error)
            if errors:
                failure = SandboxCleanupError("cannot close owned export resources")
                if primary is not None:
                    failure.add_note(f"export also failed: {primary!r}")
                for error in errors:
                    failure.add_note(repr(error))
                raise failure from errors[0]


class PrefetchChunks:
    def __init__(self, source):
        self.source = source
        self.condition = threading.Condition()
        self.queue = deque()
        self.stopped = False
        self.finished = self.eof = False
        self.queue_bytes = self.queue_peak_bytes = self.queue_peak_slots = 0
        self.receive_sec = self.producer_wait_sec = self.consumer_wait_sec = 0.0
        self.first_byte_wait_sec = None
        self.stats = ChunkStats()

    def produce(self):
        started = monotonic()
        stream = primary = None
        try:
            stream = iter(self.source.chunks())
            while True:
                with self.condition:
                    if self.stopped:
                        break
                tick = monotonic()
                try:
                    chunk = next(stream)
                except StopIteration:
                    break
                finally:
                    self.receive_sec += monotonic() - tick
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise ValueError("export transport exceeded the bounded chunk contract")
                size = chunk.nbytes if isinstance(chunk, memoryview) else len(chunk)
                if size > CHUNK_BYTES:
                    raise ValueError("export transport exceeded the bounded chunk contract")
                chunk = bytes(chunk)
                self.stats.add(chunk)
                if not chunk:
                    continue
                if self.first_byte_wait_sec is None:
                    self.first_byte_wait_sec = monotonic() - started
                with self.condition:
                    while len(self.queue) >= QUEUE_SLOTS and not self.stopped:
                        tick = monotonic()
                        self.condition.wait()
                        self.producer_wait_sec += monotonic() - tick
                    if self.stopped:
                        break
                    self.queue.append(chunk)
                    self.queue_bytes += len(chunk)
                    self.queue_peak_bytes = max(self.queue_peak_bytes, self.queue_bytes)
                    self.queue_peak_slots = max(self.queue_peak_slots, len(self.queue))
                    self.condition.notify_all()
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                if stream is not None and hasattr(stream, "close"):
                    try:
                        stream.close()
                    except BaseException as error:
                        if primary is None:
                            raise
                        primary.add_note(f"export iterator cleanup also failed: {error!r}")
            finally:
                with self.condition:
                    self.finished = True
                    self.condition.notify_all()

    def cancel(self):
        with self.condition:
            self.stopped = True
            self.queue.clear()
            self.queue_bytes = 0
            self.condition.notify_all()
        self.source.cancel()

    def __iter__(self):
        return self

    def __next__(self):
        with self.condition:
            while not self.queue and not self.finished:
                tick = monotonic()
                self.condition.wait()
                self.consumer_wait_sec += monotonic() - tick
            if not self.queue:
                self.eof = True
                raise StopIteration
            chunk = self.queue.popleft()
            self.queue_bytes -= len(chunk)
            self.condition.notify_all()
            return chunk

    def diagnostics(self):
        return {"enabled": True, "transport": "dedicated-docker-sdk-export",
                "queue_slots": QUEUE_SLOTS, "chunk_limit_bytes": CHUNK_BYTES,
                "queue_peak_slots": self.queue_peak_slots, "queue_peak_bytes": self.queue_peak_bytes,
                # Includes producer/consumer blocks and a transient previous
                # consumer view; excludes SDK buffers, manifests and Python RSS.
                "payload_bound_bytes": (QUEUE_SLOTS + 3) * CHUNK_BYTES,
                "producer_receive_sec": self.receive_sec,
                "producer_queue_wait_sec": self.producer_wait_sec,
                "consumer_queue_wait_sec": self.consumer_wait_sec,
                "first_byte_wait_sec": self.first_byte_wait_sec,
                "chunks": self.stats.snapshot(), "joined": True}


@contextmanager
def prefetch_export(container):
    chunks = PrefetchChunks(DockerExport(container))
    with capture_worker(chunks.produce, cancel=chunks.cancel, name="palinreplay-export") as worker:
        yield chunks
        if not chunks.eof:
            raise ValueError("export consumer stopped before EOF")
    worker.result()
