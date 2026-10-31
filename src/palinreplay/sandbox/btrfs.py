"""Experimental full-rootfs Docker launcher for an existing Btrfs snapshot.

Task containers use the previously validated privileged bootstrap. This
isolation policy is recorded in every State. Agent/tool exec behavior is
inherited from the Docker Sandbox.
"""

from __future__ import annotations

import time

from palinreplay.sandbox.base import (
    EnvironmentMismatch,
    SandboxCleanupError,
    SandboxError,
    SandboxInfo,
)
from palinreplay.sandbox.docker import DockerSDKSandbox, _missing
from palinreplay.storage.btrfs.pool import OWNER_LABEL, BtrfsPool


class BtrfsWorkspace:
    def __init__(self, pool, snapshot, owner):
        self.pool, self.snapshot, self.owner = pool, snapshot, owner
        self.name = "workspaces/" + owner
        self.volume_name = "pr-btrfs-root-" + owner
        self.container = None
        self.container_name = "pr-btrfs-" + owner
        self.workspace_attempted = False
        self.volume_attempted = False
        self.container_attempted = False

    def start(self, launch, bootstrap_image, *, environment, labels):
        from docker.types import Mount

        client = self.pool.client
        image = client.images.get(bootstrap_image)
        if f"{image.attrs.get('Os')}/{image.attrs.get('Architecture')}" != launch.platform:
            raise EnvironmentMismatch("Btrfs bootstrap platform differs from the saved task")
        self.pool.verify(self.snapshot)
        self.workspace_attempted = True
        self.pool.snapshot(self.snapshot.name, self.name, readonly=False)
        self.volume_attempted = True
        client.volumes.create(
            self.volume_name,
            driver="local",
            labels={OWNER_LABEL: self.owner},
            driver_opts={
                "type": "btrfs",
                "device": self.pool.device,
                "o": "noatime,subvol=" + self.name,
            },
        )
        self.container_attempted = True
        self.container = client.containers.create(
            image=bootstrap_image,
            name=self.container_name,
            labels={**labels, OWNER_LABEL: self.owner},
            platform=launch.platform,
            working_dir=launch.workdir,
            environment=environment,
            user=launch.user,
            network_mode=launch.network_mode,
            mem_limit=launch.memory_bytes,
            nano_cpus=launch.nano_cpus,
            init=True,
            privileged=True,
            mounts=[Mount("/task-root", self.volume_name, type="volume", no_copy=True)],
            detach=True,
            tty=False,
            stdin_open=False,
            auto_remove=False,
            use_config_proxy=False,
        )
        self.container.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = self.container.exec_run(["/usr/bin/stat", "-f", "-c", "%T", "/"], workdir="/")
            if result.exit_code == 0 and result.output.strip() == b"btrfs":
                return
            self.container.reload()
            if not self.container.attrs.get("State", {}).get("Running"):
                raise EnvironmentMismatch("Btrfs rootfs bootstrap exited before readiness")
            time.sleep(0.05)
        raise EnvironmentMismatch("Btrfs rootfs bootstrap did not become ready")

    def close(self):
        client = self.pool.client
        if self.container_attempted:
            try:
                container = client.containers.get(self.container_name)
            except Exception as error:
                if not _missing(error):
                    raise
            else:
                if (container.attrs.get("Config", {}).get("Labels") or {}).get(
                    OWNER_LABEL
                ) != self.owner:
                    raise SandboxCleanupError("refusing to remove an unowned Btrfs task")
                container.remove(force=True, v=True)
                try:
                    client.containers.get(container.id)
                except Exception as error:
                    if not _missing(error):
                        raise
                else:
                    raise SandboxCleanupError("Btrfs task removal unconfirmed")
            self.container_attempted = False
        if self.volume_attempted:
            try:
                volume = client.volumes.get(self.volume_name)
            except Exception as error:
                if not _missing(error):
                    raise
            else:
                if (volume.attrs.get("Labels") or {}).get(OWNER_LABEL) != self.owner:
                    raise SandboxCleanupError("refusing to remove an unowned root volume")
                volume.remove()
            self.volume_attempted = False
        if self.workspace_attempted:
            self.pool.command("delete", name=self.name)
            self.workspace_attempted = False


class BtrfsSandbox(DockerSDKSandbox):
    def __init__(self, binding, *, preload, client, network_mode):
        super().__init__(binding, client=client, network_mode=network_mode)
        self.preload = preload
        self.pool = None
        self.workspace_owner = None
        self.workspace = None
        self.mounts_at_start = None
        launch = preload.base.metadata.launch
        self._workdir, self._shell = launch.workdir, list(launch.shell)
        self._memory, self._cpus = launch.memory_bytes, launch.nano_cpus

    def start(self):
        with self._lock:
            if self.state != "new":
                raise SandboxError("Btrfs Sandbox is single-use")
            self.state = "starting"
            try:
                metadata = self.preload.base.metadata
                self.pool = BtrfsPool(self._client, metadata.snapshot.location)
                self.pool.__enter__()
                self.workspace_owner = BtrfsWorkspace(self.pool, metadata.snapshot, self._owner)
                self.workspace = self.workspace_owner.name
                self._env = self.preload.environment(self._client)
                launch = metadata.launch.model_copy(update={"network_mode": self._network})
                self.workspace_owner.start(
                    launch, metadata.bootstrap_image_id, environment=self._env, labels=self._labels
                )
                self._container = self.workspace_owner.container
                self.last_container_id = self._container.id
                self.name = self.workspace_owner.container_name
                self._daemon_platform = "linux/" + self._client.info()["Architecture"]
                self.check_root()
                self.state = "running"
                return self.inspect()
            except BaseException as error:
                self.state = "failed"
                self._cleanup_after_error(error)
                raise

    def check_root(self):
        result = self._container.exec_run(["/bin/cat", "/proc/self/mountinfo"], workdir="/")
        if result.exit_code != 0:
            raise EnvironmentMismatch("cannot check the task mount topology")
        if self.mounts_at_start is None:
            self.mounts_at_start = result.output
        elif result.output != self.mounts_at_start:
            raise EnvironmentMismatch("task mounts changed outside the supported rootfs scope")

    def inspect(self):
        if self._container is None:
            raise SandboxError("Btrfs task has no live container")
        self._container.reload()
        attrs = self._container.attrs
        config, host = attrs["Config"], attrs["HostConfig"]
        if (config.get("Labels") or {}).get(OWNER_LABEL) != self._owner:
            raise EnvironmentMismatch("Btrfs task ownership differs")
        if attrs["Image"] != self.preload.base.metadata.bootstrap_image_id:
            raise EnvironmentMismatch("Btrfs task bootstrap image differs")
        return SandboxInfo(
            run_id=self.run_id,
            task_id=self.task.spec.task_id,
            container_id=self._container.id,
            name=self.name,
            image_id=self.preload.base.metadata.source_image_id,
            actual_image_id=attrs["Image"],
            platform=self.image.platform,
            initialization="preload",
            daemon_platform=self._daemon_platform,
            status=attrs["State"]["Status"],
            workdir=config["WorkingDir"],
            shell=tuple(self._shell),
            environment=dict(self._env),
            labels=config["Labels"],
            network_mode=host["NetworkMode"],
            memory_bytes=host["Memory"],
            nano_cpus=host["NanoCpus"],
            mounts=tuple(m["Destination"] for m in attrs["Mounts"]),
        )

    def close(self):
        with self._lock:
            if self.state == "closed":
                return
            if self.workspace_owner is not None:
                self.workspace_owner.close()
            if self.pool is not None:
                self.pool.close()
            self._container = None
            self.state = "closed"
