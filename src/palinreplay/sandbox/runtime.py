"""Saved container launch configuration and environment disclosure rules."""

from typing import Annotated, Literal

from pydantic import Field

from palinreplay.core.models import RecordModel
from palinreplay.core.schema import Spec
from palinreplay.diagnostics.metrics import timed
from palinreplay.sandbox.base import SandboxError


class DockerRuntime(RecordModel):
    schema_version: Literal["palinreplay.docker-runtime.v1"] = "palinreplay.docker-runtime.v1"
    source_container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    platform: Literal["linux/amd64", "linux/arm64"]
    environment: dict[str, str]
    omitted_environment_keys: tuple[str, ...]
    workdir: str
    user: str
    network_mode: Literal["bridge", "none"]
    memory_bytes: Annotated[int, Field(strict=True, ge=0)]
    nano_cpus: Annotated[int, Field(strict=True, ge=0)]


def _environment(config: dict) -> dict[str, str]:
    values = config.get("Env") or []
    if not isinstance(values, list) or any(
        not isinstance(value, str) or "=" not in value or "\x00" in value for value in values
    ):
        raise SandboxError("invalid container environment encoding")
    result = {}
    for value in values:
        key, content = value.split("=", 1)
        if not key or key in result:
            raise SandboxError("empty or duplicate environment key")
        result[key] = content
    return result


@timed("state.runtime_check")
def _runtime(client, container, public_env_keys: frozenset[str]) -> DockerRuntime:
    container.reload()
    attrs = container.attrs
    config, host = attrs.get("Config") or {}, attrs.get("HostConfig") or {}
    if (
        attrs.get("Mounts")
        or config.get("Volumes")
        or any(
            host.get(key)
            for key in (
                "Binds",
                "Mounts",
                "Tmpfs",
                "VolumesFrom",
                "Privileged",
                "Devices",
                "DeviceRequests",
                "PortBindings",
                "PublishAllPorts",
                "CapAdd",
                "CapDrop",
                "Links",
                "PidMode",
                "UTSMode",
                "ReadonlyRootfs",
                "CpuPeriod",
                "CpuQuota",
                "CpusetCpus",
                "CpusetMems",
            )
        )
    ):
        raise SandboxError("unsupported mounts, devices or custom container runtime options")
    if host.get("IpcMode") not in (None, "", "private"):
        raise SandboxError("shared IPC namespaces are unsupported")
    image = client.images.get(attrs["Image"])
    if image.id != attrs["Image"] or (image.attrs.get("Config") or {}).get("Volumes"):
        raise SandboxError("source image identity differs or declares unsupported volumes")
    network = host.get("NetworkMode")
    networks = (attrs.get("NetworkSettings") or {}).get("Networks") or {}
    if networks and set(networks) != {network}:
        raise SandboxError("additional network attachments are unsupported")
    environment = _environment(config)
    if public_env_keys - environment.keys():
        raise SandboxError("declared public environment keys are missing from the container")
    return DockerRuntime(
        source_container_id=container.id,
        source_image_id=attrs["Image"],
        platform=f"{image.attrs.get('Os')}/{image.attrs.get('Architecture')}",
        environment={k: environment[k] for k in sorted(public_env_keys)},
        omitted_environment_keys=tuple(sorted(environment.keys() - public_env_keys)),
        workdir=config.get("WorkingDir") or "/",
        user=config.get("User") or "",
        network_mode=network,
        memory_bytes=host.get("Memory", 0),
        nano_cpus=host.get("NanoCpus", 0),
    )

class C0LaunchConfig(Spec):
    platform: Literal["linux/amd64", "linux/arm64"]
    environment: dict[str, str]
    omitted_environment_keys: tuple[str, ...]
    workdir: str
    user: str
    shell: tuple[str, ...]
    entrypoint: tuple[str, ...]
    command: tuple[str, ...]
    network_mode: Literal["bridge", "none"]
    memory_bytes: Annotated[int, Field(strict=True, ge=0)]
    nano_cpus: Annotated[int, Field(strict=True, ge=0)]


PUBLIC_ENV_KEYS = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "HOME",
        "BASH_ENV",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
        "CONDA_DEFAULT_ENV",
        "CONDA_EXE",
        "CONDA_PYTHON_EXE",
        "CONDA_SHLVL",
        "CONDA_PREFIX",
    }
)
