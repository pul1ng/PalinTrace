"""Own a Btrfs image-backed pool and its management container."""

from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path
from uuid import UUID, uuid4

from palinreplay.core.errors import IntegrityError
from palinreplay.sandbox.base import EnvironmentMismatch, SandboxCleanupError, SandboxError
from palinreplay.storage.btrfs.models import PoolLocation

DEFAULT_HELPER_IMAGE = "palinreplay/btrfs-tools:0.1"
DEFAULT_BOOTSTRAP_IMAGE = "palinreplay/btrfs-bootstrap:0.1"
POOL_LABEL = "palinreplay.btrfs.pool"
OWNER_LABEL = "palinreplay.btrfs.owner"
_ACTIVE = {}


def _upload(path, name):
    """One file tar, streamed with bounded memory (including multi-GB inputs)."""
    with Path(path).open("rb") as source:
        size = os.fstat(source.fileno()).st_size
        member = tarfile.TarInfo(name)
        member.size, member.mode = size, 0o600
        yield member.tobuf(format=tarfile.PAX_FORMAT)
        remaining = size
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                raise IntegrityError("truncated Btrfs input transfer")
            remaining -= len(chunk)
            yield chunk
        if source.read(1):
            raise IntegrityError("Btrfs input changed during transfer")
        yield bytes((-size % 512) + 1024)


class BtrfsPool:
    """Own only a helper/loop mount; persistent volume and snapshots are retained."""

    def __init__(self, client, location, *, fresh=False):
        self.client, self.location, self.fresh = client, location, fresh
        self.container = None
        self.owner = uuid4().hex
        self.name = "pr-btrfs-pool-" + location.uuid.hex
        self.device = None
        self.closed = False
        self.started = False
        self.initial_uploads = ()
        self.key = (location.daemon_id, location.volume)

    @classmethod
    def create(cls, client):
        identity = uuid4()
        helper = client.images.get(DEFAULT_HELPER_IMAGE)
        daemon = client.info()
        if daemon.get("OSType") != "linux":
            raise EnvironmentMismatch("Btrfs requires a Linux Docker daemon")
        location = PoolLocation(
            volume="palinreplay-btrfs-" + identity.hex,
            uuid=identity,
            daemon_id=daemon["ID"],
            helper_image_id=helper.id,
        )
        client.volumes.create(name=location.volume, labels={POOL_LABEL: str(identity)})
        return cls(client, location, fresh=True)

    def __enter__(self):
        if self.key in _ACTIVE:
            raise SandboxError("this Btrfs pool already has an active owner")
        if self.client.info().get("ID") != self.location.daemon_id:
            raise IntegrityError("Btrfs pool belongs to another Docker daemon")
        volume = self.client.volumes.get(self.location.volume)
        if (volume.attrs.get("Labels") or {}).get(POOL_LABEL) != str(self.location.uuid):
            raise IntegrityError("Btrfs volume identity differs from its saved reference")
        self.client.images.get(self.location.helper_image_id)
        try:
            self.container = self.client.containers.create(
                self.location.helper_image_id,
                name=self.name,
                labels={OWNER_LABEL: self.owner, POOL_LABEL: str(self.location.uuid)},
                command=["sleep", "infinity"],
                entrypoint=[],
                init=True,
                privileged=True,
                network_mode="none",
                volumes={self.location.volume: {"bind": "/storage", "mode": "rw"}},
                detach=True,
                use_config_proxy=False,
            )
            self.container.start()
            # Upload only the trusted packaged worker, never user code.
            body = Path(__file__).with_name("worker.py").read_bytes()
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as archive:
                member = tarfile.TarInfo("palin-btrfs-worker.py")
                member.size, member.mode = len(body), 0o400
                archive.addfile(member, io.BytesIO(body))
            self.container.put_archive("/", stream.getvalue())
            self.started = True
            # Finish ordinary archive transfers before mounting the private
            # filesystem; all runtime snapshot operations use exec afterwards.
            for path, name in self.initial_uploads:
                self.upload(path, name)
            result = self.command("setup", fresh=self.fresh, uuid=str(self.location.uuid))
            self.device = result["device"]
            _ACTIVE[self.key] = self
            return self
        except BaseException as error:
            try:
                self.close()
            except Exception as cleanup:
                error.add_note(f"Btrfs helper cleanup unconfirmed: {cleanup}")
            raise

    def command(self, op, **values):
        response = self.container.exec_run(
            ["python3", "/palin-btrfs-worker.py", json.dumps({"op": op, **values})],
        )
        try:
            value = json.loads(response.output)
        except (ValueError, TypeError) as error:
            raise SandboxError("Btrfs worker returned no definitive result") from error
        if response.exit_code or not value.get("ok"):
            raise IntegrityError("Btrfs operation failed: " + value.get("error", op))
        return value["result"]

    def upload(self, path, name):
        if not self.container.put_archive("/storage", _upload(path, name)):
            raise SandboxError("Btrfs input transfer was rejected")

    def snapshot(self, source, destination, *, readonly=True):
        result = self.command("snapshot", source=source, destination=destination, readonly=readonly)
        if result["readonly"] != readonly:
            raise IntegrityError("snapshot has unexpected write permissions")
        return result

    def verify(self, snapshot):
        if snapshot.location != self.location:
            raise IntegrityError("snapshot references another pool")
        actual = self.command("info", name=snapshot.name)
        if not actual["readonly"] or UUID(actual["uuid"]) != snapshot.uuid:
            raise IntegrityError("snapshot identity or read-only property differs")

    def close(self):
        if self.closed:
            return
        if self.container is not None:
            self.container.reload()
            if (self.container.attrs.get("Config", {}).get("Labels") or {}).get(
                OWNER_LABEL
            ) != self.owner:
                raise SandboxCleanupError("refusing to close an unowned Btrfs helper")
            if self.started:
                self.command("close")
            self.container.remove(force=True, v=True)
            try:
                self.client.containers.get(self.container.id)
            except Exception as error:
                if getattr(error, "status_code", None) != 404:
                    raise
            else:
                raise SandboxCleanupError("Btrfs helper removal unconfirmed")
            if self.device is not None:
                # Confirm lazy loop destruction after the runtime has released
                # this container's remaining filesystem references. This helper
                # only reads sysfs; it cannot detach or modify another device.
                script = (
                    "import sys; from pathlib import Path; "
                    'p=Path("/sys/block")/sys.argv[1]/"loop/backing_file"; '
                    "sys.exit(1 if p.exists() and p.read_text().strip() else 0)"
                )
                try:
                    self.client.containers.run(
                        self.location.helper_image_id,
                        ["python3", "-c", script, Path(self.device).name],
                        name="pr-btrfs-detach-" + self.owner,
                        labels={OWNER_LABEL: self.owner},
                        network_mode="none",
                        remove=True,
                        use_config_proxy=False,
                    )
                except Exception as error:
                    raise SandboxCleanupError(
                        "owned Btrfs loop device release unconfirmed"
                    ) from error
        if _ACTIVE.get(self.key) is self:
            _ACTIVE.pop(self.key)
        self.closed = True

    def discard(self):
        self.close()
        volume = self.client.volumes.get(self.location.volume)
        if (volume.attrs.get("Labels") or {}).get(POOL_LABEL) != str(self.location.uuid):
            raise SandboxCleanupError("refusing to remove an unowned Btrfs volume")
        volume.remove()

    def __exit__(self, *exc):
        self.close()
