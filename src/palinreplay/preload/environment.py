"""Acquire a real image and an owned, not-yet-started preload container.

Usage::

    with create_preload_environment(inputs) as environment:
        # The next initialization step can inspect/start environment.container.
        print(environment.container_id, environment.image_id)

Pull only for a missing image or requested platform. Local IDs must match exactly
after pulling from a provenance hint; registry digests are resolved by Docker,
then pinned to the returned local image ID. Image caches are retained; owned
containers are removed on success, error or cancellation. This step never
executes instruction.
"""

import math
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from palinreplay.core.errors import RecordUsageError
from palinreplay.preload.input import PreloadInput
from palinreplay.sandbox.base import EnvironmentMismatch, SandboxCleanupError, SandboxError
from palinreplay.sandbox.client import connect_docker
from palinreplay.sandbox.runtime import PUBLIC_ENV_KEYS, _environment

OWNER_LABEL = "palinreplay.preload.owner"
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class PreloadEnvironment:
    image_id: str
    platform: str
    container_id: str
    name: str
    pulled_from: str | None
    container: Any = field(repr=False, compare=False)


class PreloadCleanupError(SandboxCleanupError):
    def __init__(self, name: str, container_id: str | None):
        self.container_name, self.container_id = name, container_id
        super().__init__(f"preload container removal unconfirmed: {container_id or name}")


def _missing(error):
    return getattr(error, "status_code", None) == 404


def _check_image(image, inputs):
    if not _IMAGE_ID.fullmatch(image.id):
        raise EnvironmentMismatch("Docker did not resolve a complete local image ID")
    if _IMAGE_ID.fullmatch(inputs.image) and image.id != inputs.image:
        raise EnvironmentMismatch("resolved image ID does not match preload input")
    actual = image.attrs
    if f"{actual.get('Os')}/{actual.get('Architecture')}" != inputs.platform:
        raise EnvironmentMismatch("resolved image platform does not match preload input")
    if (actual.get("Config") or {}).get("Volumes"):
        raise EnvironmentMismatch("preload MVP does not support image-declared volumes")


def _resolve_image(client, inputs, allow_pull):
    image = None
    try:
        image = client.images.get(inputs.image)
    except Exception as error:
        if not _missing(error):
            raise SandboxError("cannot inspect the requested preload image") from error
    # A multi-platform registry digest may be present only for another platform.
    # A local image ID, in contrast, fixes one particular image configuration.
    wrong_platform = image is not None and (
        f"{image.attrs.get('Os')}/{image.attrs.get('Architecture')}" != inputs.platform
    )
    if image is not None and not (wrong_platform and "@" in inputs.image and allow_pull):
        _check_image(image, inputs)
        return image, None
    if not allow_pull:
        raise SandboxError("preload image is unavailable locally and pulling is disabled")
    reference = inputs.image
    if _IMAGE_ID.fullmatch(reference):
        reference = next(iter(inputs.repo_digests), None) or inputs.image_reference
        if not reference or _IMAGE_ID.fullmatch(reference):
            raise SandboxError(
                "missing local image ID requires a repository digest or image_reference to pull"
            )
    try:
        image = client.images.pull(reference, platform=inputs.platform)
    except Exception as error:
        raise SandboxError("preload image pull failed; check registry access and platform") from error
    _check_image(image, inputs)
    return image, reference


def _check_container(container, image_id, inputs, owner):
    container.reload()
    attrs = container.attrs
    config = attrs.get("Config") or {}
    if (config.get("Labels") or {}).get(OWNER_LABEL) != owner:
        raise EnvironmentMismatch("preload container ownership does not match")
    if attrs.get("Image") != image_id or config.get("Image") != image_id:
        raise EnvironmentMismatch("preload container was not created from the pinned image ID")
    if attrs.get("State", {}).get("Status") != "created":
        raise EnvironmentMismatch("preload container must remain unstarted until initialization")
    if attrs.get("Mounts") or config.get("Volumes"):
        raise EnvironmentMismatch("unexpected mounts in preload container")
    if config.get("WorkingDir") != inputs.workdir:
        raise EnvironmentMismatch("preload container workdir does not match input")
    values = config.get("Env") or []
    if any(not isinstance(value, str) or "=" not in value for value in values):
        raise EnvironmentMismatch("invalid preload container environment")
    environment = dict(value.split("=", 1) for value in values)
    if len(environment) != len(values) or any(
        environment.get(key) != value for key, value in inputs.env.items()
    ):
        raise EnvironmentMismatch("preload container environment does not match input")


def _remove_owned(client, container, name, owner):
    try:
        if container is None:
            container = client.containers.get(name)
        container.reload()
    except Exception as error:
        if _missing(error):
            return
        raise
    if (container.attrs.get("Config", {}).get("Labels") or {}).get(OWNER_LABEL) != owner:
        raise SandboxCleanupError("refusing to remove an unowned preload container")
    try:
        container.remove(force=True, v=True)
    except Exception as error:
        if not _missing(error):
            raise
    try:
        client.containers.get(container.id)
    except Exception as error:
        if _missing(error):
            return
        raise
    raise SandboxCleanupError("preload container still exists after removal")


@contextmanager
def create_preload_environment(
    inputs: PreloadInput, *, client=None, allow_pull: bool = True,
    network_mode: str = "bridge", api_timeout_sec: float = 600,
):
    """Yield an owned container; release it before leaving the context.

    A supplied Docker client remains caller-owned. Created containers use the
    existing Sandbox's 2 GiB / 2 CPU defaults, with no host mounts or published
    ports. The container is not started and its original entrypoint is replaced
    by an idle command for the next initialization step.
    """
    if not isinstance(inputs, PreloadInput):
        raise TypeError("inputs must be a PreloadInput")
    inputs = PreloadInput.model_validate(inputs.model_dump())
    if type(allow_pull) is not bool:
        raise ValueError("allow_pull must be boolean")
    if network_mode not in ("bridge", "none"):
        raise ValueError("network_mode must be bridge or none")
    if (isinstance(api_timeout_sec, bool) or not isinstance(api_timeout_sec, (int, float))
            or not math.isfinite(api_timeout_sec) or api_timeout_sec <= 0):
        raise ValueError("api_timeout_sec must be a positive finite number")
    owner = uuid4().hex
    name = "palinreplay-preload-" + owner
    owns_client = client is None
    container = primary = None
    create_attempted = False
    try:
        if client is None:
            client = connect_docker(api_timeout_sec)
        if client.info().get("OSType") != "linux":
            raise EnvironmentMismatch("preload requires a Linux Docker daemon")
        image, pulled_from = _resolve_image(client, inputs, allow_pull)
        labels = {OWNER_LABEL: owner}
        if inputs.task_id is not None:
            labels["palinreplay.preload.task"] = inputs.task_id
        create_attempted = True
        container = client.containers.create(
            image=image.id, name=name, labels=labels,
            entrypoint=["sleep"], command=["infinity"],
            init=True,
            working_dir=inputs.workdir, environment=dict(inputs.env), platform=inputs.platform,
            network_mode=network_mode, mem_limit=2 * 1024**3, nano_cpus=2_000_000_000,
            detach=True, tty=False, stdin_open=False, privileged=False,
            auto_remove=False, volumes={}, ports={}, use_config_proxy=False,
        )
        _check_container(container, image.id, inputs, owner)
        yield PreloadEnvironment(
            image_id=image.id, platform=inputs.platform, container_id=container.id,
            name=name, pulled_from=pulled_from, container=container,
        )
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup = None
        if create_attempted:
            try:
                _remove_owned(client, container, name, owner)
            except Exception as error:
                cleanup = PreloadCleanupError(name, getattr(container, "id", None))
                cleanup.__cause__ = primary or error
        if owns_client and client is not None:
            try:
                client.close()
            except Exception as error:
                if cleanup is None and primary is None:
                    raise SandboxCleanupError("cannot close preload Docker client") from error
                (cleanup or primary).add_note("Docker client close also failed")
        if cleanup is not None:
            raise cleanup


def public_keys(inputs, extra):
    # Keep the same disclosure policy as record, without importing optional
    # Agent implementations on the help/input-validation paths.

    client = connect_docker(600)
    primary = None
    try:
        if client.info().get("OSType") != "linux":
            raise EnvironmentMismatch("preload requires a Linux Docker daemon")
        image, _ = _resolve_image(client, inputs, allow_pull=True)
        keys = _environment(image.attrs.get("Config") or {}).keys() | inputs.env.keys()
        if set(extra) - keys:
            raise RecordUsageError("--public-env key is absent from the image and input environment")
        return tuple(sorted((keys & PUBLIC_ENV_KEYS) | set(extra)))
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            client.close()
        except Exception as error:
            if primary is None:
                raise SandboxCleanupError("cannot close preload image inspection client") from error
            primary.add_note("Docker image inspection client close also failed")
