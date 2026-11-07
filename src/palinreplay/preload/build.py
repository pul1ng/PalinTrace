"""One entry point for input loading, environment acquisition, initialization and C0 freezing.

Example::

    result = build_preload(
        "data/tasks/lite-one", "data/preloads/first",
        initialization=InitializationSpec(checks=("python -V",)),
        public_env_keys=("PATH", "BASH_ENV"),
    )
    print(result.preload_id, result.path)

A source directory is an existing task package with images.json; a source file
is standalone preload-input JSON. PreloadInput objects are also accepted.
C0 is stored as a read-only Btrfs snapshot in an image-backed Docker volume.
The ID is ``preload:sha256:<C0 metadata checksum>``. It binds the selected base
and launch settings, not a temporary container or output directory.
It can be reconstructed from c0.checksum.json without an additional registry.
"""

import hashlib
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import palinreplay.storage.rootfs as _rootfs
from palinreplay.core.serialization import json_bytes
from palinreplay.preload.environment import create_preload_environment
from palinreplay.preload.freeze import _snapshot
from palinreplay.preload.initialization import InitializationSpec, initialize_preload_environment
from palinreplay.preload.input import PreloadInput, load_preload_input, load_preload_task
from palinreplay.sandbox.base import EnvironmentMismatch, SandboxCleanupError
from palinreplay.sandbox.client import connect_docker
from palinreplay.storage.btrfs.models import BtrfsC0, Snapshot
from palinreplay.storage.btrfs.pool import DEFAULT_BOOTSTRAP_IMAGE, BtrfsPool
from palinreplay.storage.io import export_directory, write_json

__all__ = ["PreloadResult", "build_preload"]


@dataclass(frozen=True)
class PreloadResult:
    preload_id: str
    path: Path
    duration_sec: float


def _load_source(source, task_id):
    if isinstance(source, PreloadInput):
        inputs = PreloadInput.model_validate(source.model_dump())
    elif isinstance(source, (str, Path)):
        path = Path(source)
        inputs = load_preload_task(path, task_id) if path.is_dir() else load_preload_input(path)
    else:
        raise TypeError("source must be PreloadInput, a preload JSON file or a task package path")
    if task_id is not None and inputs.task_id != task_id:
        raise ValueError("task_id does not match the preload input")
    return inputs


def build_preload(
    source: PreloadInput | str | Path, output: str | Path, *,
    initialization: InitializationSpec, public_env_keys: tuple[str, ...],
    task_id: str | None = None, client=None, allow_pull: bool = True,
    network_mode: str = "bridge", api_timeout_sec: float = 600,
    stop_timeout_sec: int = 5,
) -> PreloadResult:
    """Build and publish a new preload, returning its content ID and absolute path.

    All four existing steps are reused. C0 stays private until container/client
    cleanup succeeds. Existing outputs are never overwritten. A supplied Docker
    client remains caller-owned. duration_sec includes this function's input
    validation, build, cleanup and final publication, but not Python imports.
    """
    started = time.monotonic()
    if not isinstance(initialization, InitializationSpec):
        raise TypeError("initialization must be an InitializationSpec")
    initialization = InitializationSpec.model_validate(initialization.model_dump())
    if (not isinstance(public_env_keys, tuple)
            or any(not isinstance(key, str) or not key or "=" in key or "\x00" in key
                   for key in public_env_keys)
            or len(set(public_env_keys)) != len(public_env_keys)):
        raise ValueError("public_env_keys must be a tuple of unique environment names")
    if type(stop_timeout_sec) is not int or not 0 <= stop_timeout_sec <= 60:
        raise ValueError("stop_timeout_sec must be an integer between 0 and 60")
    inputs = _load_source(source, task_id)
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"preload output already exists: {output}")

    result = _build_preload(
        inputs, output, initialization=initialization, public_env_keys=public_env_keys,
        client=client, allow_pull=allow_pull, network_mode=network_mode,
        api_timeout_sec=api_timeout_sec, stop_timeout_sec=stop_timeout_sec,
    )
    return PreloadResult(result.preload_id, result.path, time.monotonic() - started)


def _build_preload(
    inputs,
    output,
    *,
    initialization,
    public_env_keys,
    client,
    allow_pull,
    network_mode,
    api_timeout_sec,
    stop_timeout_sec,
):

    started = time.monotonic()
    owned_client = client is None
    pool = None
    primary = None
    try:
        if client is None:
            client = connect_docker(api_timeout_sec)
        bootstrap = client.images.get(DEFAULT_BOOTSTRAP_IMAGE)
        if f"{bootstrap.attrs.get('Os')}/{bootstrap.attrs.get('Architecture')}" != inputs.platform:
            raise EnvironmentMismatch("Btrfs bootstrap image platform differs from the task")
        with export_directory(output) as stage:
            with create_preload_environment(
                inputs,
                client=client,
                allow_pull=allow_pull,
                network_mode=network_mode,
                api_timeout_sec=api_timeout_sec,
            ) as environment:
                initialized = initialize_preload_environment(environment, inputs, initialization)
                container = environment.container
                owner = environment.name.removeprefix("palinreplay-preload-")
                launch, _ = _snapshot(environment, inputs, public_env_keys, owner)
                if launch.user not in ("", "0", "root", "0:0"):
                    raise EnvironmentMismatch("Btrfs MVP supports root task users only")
                for check in initialized.checks:
                    result = container.exec_run(
                        [*inputs.shell, check.command],
                        workdir=inputs.workdir,
                        environment=inputs.env,
                    )
                    if result.exit_code != 0:
                        raise EnvironmentMismatch("C0 readiness check failed before freeze")
                container.stop(timeout=stop_timeout_sec)
                frozen_launch, stamp = _snapshot(
                    environment, inputs, public_env_keys, owner, stopped=True
                )
                if launch != frozen_launch:
                    raise EnvironmentMismatch("launch configuration changed while stopping C0")
                pool = BtrfsPool.create(client)
                with tempfile.TemporaryDirectory(prefix="palin-btrfs-import-") as temporary:
                    archive = Path(temporary) / "rootfs.tar"
                    inventory = _rootfs.export_inventory(container, archive, hash_archive=False)
                    if _snapshot(environment, inputs, public_env_keys, owner, stopped=True) != (
                        launch,
                        stamp,
                    ):
                        raise EnvironmentMismatch("C0 source changed during export")
                    oracle = Path(temporary) / "expected-C0.json"
                    oracle.write_bytes(json_bytes(inventory.logical))
                    pool.initial_uploads = ((archive, "import.tar"), (oracle, "expected-C0.json"))
                    pool.__enter__()
                    import_error = None
                    try:
                        metadata, build = _import_c0(
                            pool, environment, inputs, initialized, launch, bootstrap, inventory
                        )
                    except Exception as error:
                        import_error = error
                        raise
                    finally:
                        try:
                            pool.close()
                        except Exception as cleanup:
                            if import_error is None:
                                raise
                            import_error.add_note(f"Btrfs pool close also failed: {cleanup!r}")
                    write_json(stage / "build.json", build)
            # Source and helper cleanup must finish before publication.
            write_json(stage / "c0.json", metadata.model_dump(mode="json"))
            digest = hashlib.sha256((stage / "c0.json").read_bytes()).hexdigest()
            write_json(stage / "c0.checksum.json", {"sha256": digest})
            for path in stage.iterdir():
                path.chmod(0o400)
            if owned_client:
                owned_client = False
                try:
                    client.close()
                except Exception as error:
                    raise SandboxCleanupError("cannot close Btrfs preload Docker client") from error
        return PreloadResult(
            "preload:sha256:" + digest, Path(output).absolute(), time.monotonic() - started
        )
    except BaseException as error:
        primary = error
        if pool is not None:
            try:
                pool.discard()
            except Exception as cleanup:
                if not isinstance(error, Exception):
                    # A cleanup fault still takes precedence over cancellation.
                    primary = cleanup
                    raise
                error.add_note(f"Btrfs pool discard also failed: {cleanup!r}")
        raise
    finally:
        if owned_client and client is not None:
            try:
                client.close()
            except Exception as cleanup:
                if primary is None or not isinstance(primary, Exception):
                    raise
                primary.add_note(f"Docker client close also failed: {cleanup!r}")


def _import_c0(pool, environment, inputs, initialized, launch, bootstrap, inventory):
    workspace = "workspaces/" + uuid4().hex
    imported = pool.command("import", workspace=workspace)
    saved = pool.snapshot(workspace, "bases/C0")
    pool.command("delete", name=workspace)
    metadata = BtrfsC0(
        task_id=inputs.task_id,
        source_image_id=environment.image_id,
        input_sha256=initialized.input_sha256,
        initialization_sha256=initialized.initialization_sha256,
        oracle_sha256=imported["oracle_sha256"],
        bootstrap_image_id=bootstrap.id,
        snapshot=Snapshot(location=pool.location, name="bases/C0", uuid=saved["uuid"]),
        launch=launch,
    )
    return metadata, {
        "docker_exports": 1,
        "full_tree_scans": 1,
        "source_container_id": environment.container_id,
        "imported_paths": imported["paths"],
        "roundtrip_verified": True,
        "export_bytes": inventory.size,
        "storage": pool.command("usage"),
    }
