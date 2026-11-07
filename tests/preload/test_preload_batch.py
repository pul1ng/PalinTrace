import json

import pytest

import palinreplay.preload.batch as preload_batch
import palinreplay.preload.build as preload_build
import palinreplay.preload.environment as preload_environment
from palinreplay.preload.batch import select_preload_inputs
from palinreplay.preload.build import PreloadResult
from palinreplay.preload.initialization import InitializationSpec
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


def test_batch_builds_serially_with_per_input_environment(batch_package, tmp_path, monkeypatch):
    selected = select_preload_inputs(
        batch_package, ["example__repo-3", "example__repo-1", "example__repo-2"],
    )
    selected = [item.model_copy(update={"env": {f"TASK_{index}": str(index)}})
                for index, item in enumerate(selected)]
    output = tmp_path / "new" / "batch"
    paths = [output / item.task_id for item in selected]
    initialization = InitializationSpec(commands=("prepare",), checks=("ready",))
    events = []

    def keys(item, extra):
        events.append(("inspect", item.task_id, dict(item.env), tuple(extra)))
        return tuple(item.env) + tuple(extra)

    def build(item, path, **kwargs):
        index = len([event for event in events if event[0] == "done"])
        assert events[-1][0] == "inspect"
        assert item is selected[index] and path == paths[index]
        assert kwargs == {
            "initialization": initialization,
            "public_env_keys": (f"TASK_{index}", "EXPLICIT"),
        }
        events.append(("start", item.task_id))
        result = PreloadResult(f"preload:{item.task_id}", path.absolute(), index + 0.5)
        events.append(("done", item.task_id))
        return result

    monkeypatch.setattr(preload_batch, "public_keys", keys)
    monkeypatch.setattr(preload_batch, "build_preload", build)
    results = preload_batch.build_preloads(
        batch_package, selected, output, paths, initialization=initialization,
        public_env=["EXPLICIT"],
    )
    assert output.is_dir()
    assert [event[:2] for event in events] == [
        (kind, item.task_id) for item in selected for kind in ("inspect", "start", "done")
    ]
    assert [result.path for result in results] == [path.absolute() for path in paths]


def test_index_tracks_only_returned_results_and_relative_paths(
    batch_package, tmp_path, monkeypatch,
):
    selected = select_preload_inputs(batch_package)[:2]
    output = tmp_path / "batch"
    paths = [output / item.task_id for item in selected]
    snapshots = []
    write_json = preload_batch.write_json

    def record_index(path, data, **kwargs):
        write_json(path, data, **kwargs)
        snapshots.append(json.loads(path.read_text()))

    def build(item, path, **kwargs):
        index = json.loads((output / "preloads.json").read_text())
        entry = next(entry for entry in index["tasks"] if entry["task_id"] == item.task_id)
        assert entry["status"] == "building" and entry["preload_id"] is None
        path.mkdir()
        (path / "c0.json").write_text(item.task_id)
        return PreloadResult("preload:" + item.task_id, path, 1.25)

    monkeypatch.setattr(preload_batch, "write_json", record_index)
    monkeypatch.setattr(preload_batch, "public_keys", lambda item, extra: ())
    monkeypatch.setattr(preload_batch, "build_preload", build)
    results = preload_batch.build_preloads(
        batch_package, selected, output, paths,
        initialization=InitializationSpec(checks=("ready",)), public_env=[],
    )
    assert [entry["status"] for entry in snapshots[0]["tasks"]] == ["pending", "pending"]
    assert any([entry["status"] for entry in snapshot["tasks"]] == ["completed", "pending"]
               for snapshot in snapshots)
    final = json.loads((output / "preloads.json").read_text())
    assert final["source"] == str(batch_package.absolute()) and final["status"] == "completed"
    for entry, item, result in zip(final["tasks"], selected, results, strict=True):
        assert entry == {
            "task_id": item.task_id, "status": "completed", "preload_id": result.preload_id,
            "path": item.task_id, "duration_sec": result.duration_sec,
        }
        assert (output / entry["path"] / "c0.json").read_text() == item.task_id
