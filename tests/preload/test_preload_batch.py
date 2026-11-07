import json

import pytest

import palinreplay.preload.build as preload_build
import palinreplay.preload.environment as preload_environment
from palinreplay.preload.batch import select_preload_inputs
from palinreplay.tasks import convert_rows
from palinreplay.tasks.images import PreparedImage, PreparedImages
from palinreplay.tasks.model import DatasetSource, file_sha256
from palinreplay.tasks.sources import resolve
from tests.preload.test_preload_input import inputs
from tests.tasks.test_images import IMAGE_ID, REPO_DIGEST


@pytest.fixture
def batch_package(tmp_path, row):
    source = tmp_path / "dataset"
    task_ids = ["example__repo-1", "example__repo-2", "example__repo-3"]
    rows = [row | {"instance_id": task_id, "image_name": "registry.example/task:v1"}
            for task_id in task_ids]
    convert_rows(
        rows, source, adapter=resolve("swebench_lite"),
        source=DatasetSource(source="swebench_lite", dataset="unit/test"),
    )
    prepared = PreparedImages(
        dataset_manifest_sha256=file_sha256(source / "manifest.json"),
        images=[PreparedImage(
            reference="registry.example/task:v1", platform="linux/amd64", image_id=IMAGE_ID,
            repo_digests=[REPO_DIGEST], task_ids=task_ids,
        )],
    )
    (source / "images.json").write_text(prepared.model_dump_json())
    return source


@pytest.fixture
def no_docker(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("task selection must not create or inspect Docker resources")

    monkeypatch.setattr(preload_build, "connect_docker", forbidden)
    monkeypatch.setattr(preload_environment, "connect_docker", forbidden)


def test_package_selects_manifest_order_and_requested_order(batch_package, no_docker):
    assert [item.task_id for item in select_preload_inputs(batch_package)] == [
        "example__repo-1", "example__repo-2", "example__repo-3",
    ]
    assert [item.task_id for item in select_preload_inputs(
        batch_package, ["example__repo-3", "example__repo-1"],
    )] == ["example__repo-3", "example__repo-1"]


@pytest.mark.parametrize("task_ids, error", [
    (["example__repo-1", "example__repo-1"], "duplicate"),
    (["example__repo-1", "missing"], "unknown"),
])
def test_invalid_package_selection_fails_before_docker(batch_package, no_docker, task_ids, error):
    with pytest.raises(ValueError, match=error):
        select_preload_inputs(batch_package, task_ids)


def test_standalone_selection_keeps_identity_rules(tmp_path, no_docker):
    source = tmp_path / "input.json"
    source.write_text(json.dumps(inputs(task_id="example__repo-1")))
    assert select_preload_inputs(source)[0].task_id == "example__repo-1"
    assert select_preload_inputs(source, ["example__repo-1"])[0].task_id == "example__repo-1"
    with pytest.raises(ValueError, match="does not match"):
        select_preload_inputs(source, ["example__repo-2"])
    with pytest.raises(ValueError, match="at most one"):
        select_preload_inputs(source, ["example__repo-1", "example__repo-2"])
    source.write_text(json.dumps(inputs()))
    with pytest.raises(ValueError, match="does not match"):
        select_preload_inputs(source, ["example__repo-1"])
