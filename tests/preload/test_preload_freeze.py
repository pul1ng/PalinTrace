"""Stable source and metadata checks used by Btrfs preload freezing."""

import pytest

from palinreplay.preload.environment import create_preload_environment
from palinreplay.preload.freeze import _snapshot
from palinreplay.sandbox.base import EnvironmentMismatch, SandboxError
from palinreplay.storage.io import _read_metadata
from palinreplay.storage.store import IntegrityError
from tests.preload.test_preload import runtime as runtime
from tests.preload.test_preload_environment import inputs


def test_launch_snapshot_keeps_only_public_values_and_records_reinjection_names(runtime):
    source = inputs(env={"LANG": "C.UTF-8", "PRIVATE_TOKEN": "must-not-save"})
    with create_preload_environment(source, client=runtime[0]) as environment:
        owner = environment.name.removeprefix("palinreplay-preload-")
        launch, _ = _snapshot(environment, source, ("LANG", "PATH"), owner)
        assert launch.environment == {"LANG": "C.UTF-8", "PATH": "/usr/bin"}
        assert launch.omitted_environment_keys == ("PRIVATE_TOKEN",)
        assert "must-not-save" not in launch.model_dump_json()


@pytest.mark.parametrize("fault", ["owner", "image", "workdir", "environment", "entrypoint", "mount"])
def test_source_drift_is_rejected_before_btrfs_import(runtime, fault):
    source = inputs()
    with create_preload_environment(source, client=runtime[0]) as environment:
        owner = environment.name.removeprefix("palinreplay-preload-")
        attrs = environment.container.attrs
        if fault == "owner":
            attrs["Config"]["Labels"]["palinreplay.preload.owner"] = "other"
        elif fault == "image":
            attrs["Image"] = "sha256:" + "b" * 64
        elif fault == "workdir":
            attrs["Config"]["WorkingDir"] = "/other"
        elif fault == "environment":
            attrs["Config"]["Env"] = ["LANG=changed", "PATH=/usr/bin"]
        elif fault == "entrypoint":
            attrs["Config"]["Entrypoint"] = ["other"]
        else:
            attrs["Mounts"] = [{"Destination": "/testbed"}]
        with pytest.raises((EnvironmentMismatch, SandboxError)):
            _snapshot(environment, source, ("LANG", "PATH"), owner)
        if fault == "owner":
            attrs["Config"]["Labels"]["palinreplay.preload.owner"] = owner


def test_stopped_snapshot_rejects_a_live_writer(runtime):
    source = inputs()
    with create_preload_environment(source, client=runtime[0]) as environment:
        owner = environment.name.removeprefix("palinreplay-preload-")
        environment.container.attrs["State"].update(Status="running", Running=True)
        with pytest.raises(EnvironmentMismatch, match="remain stopped"):
            _snapshot(environment, source, ("LANG", "PATH"), owner, stopped=True)


@pytest.mark.parametrize("kind", ["symlink", "directory", "oversized", "missing"])
def test_metadata_must_be_a_bounded_regular_file(tmp_path, kind):
    path = tmp_path / "c0.json"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text("{}")
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    elif kind == "oversized":
        path.write_bytes(bytes(2 * 1024**2 + 1))
    with pytest.raises(IntegrityError):
        _read_metadata(path)
