import pytest

import palinreplay.cli.outputs as outputs
from palinreplay.cli.preload import preload_output_paths
from palinreplay.core.errors import RecordUsageError
from palinreplay.preload.batch import select_preload_inputs
from tests.preload.test_preload_batch import batch_package as batch_package
from tests.preload.test_preload_batch import no_docker as no_docker


def test_default_output_names_follow_single_and_batch_conventions(
    tmp_path, batch_package, no_docker, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(outputs, "timestamp", lambda: "261107_123456")
    selected = select_preload_inputs(batch_package)
    root, paths = preload_output_paths(batch_package, selected[:1])
    assert root == tmp_path / "outputs/preload/261107_123456_example__repo-1"
    assert paths == [root]
    root, paths = preload_output_paths(batch_package, selected[:2])
    assert root == tmp_path / "outputs/preload/261107_123456_dataset"
    assert paths == [root / "example__repo-1", root / "example__repo-2"]
    assert not root.exists()


def test_custom_output_is_single_root_or_distinct_batch_children(tmp_path, batch_package, no_docker):
    selected = select_preload_inputs(batch_package)
    output = tmp_path / "custom"
    assert preload_output_paths(batch_package, selected[:1], output) == (output, [output])
    assert preload_output_paths(batch_package, selected[1:], output) == (
        output, [output / "example__repo-2", output / "example__repo-3"],
    )
    assert not output.exists()


@pytest.mark.parametrize("kind", ["directory", "file", "dangling-symlink"])
def test_existing_output_is_untouched_before_any_build(
    tmp_path, batch_package, no_docker, kind,
):
    selected = select_preload_inputs(batch_package)
    output = tmp_path / "taken"
    if kind == "directory":
        output.mkdir()
        (output / "sentinel").write_text("keep")
    elif kind == "file":
        output.write_text("keep")
    else:
        output.symlink_to(tmp_path / "missing")
    with pytest.raises(FileExistsError):
        preload_output_paths(batch_package, selected, output)
    if kind == "directory":
        assert (output / "sentinel").read_text() == "keep"
    elif kind == "file":
        assert output.read_text() == "keep"
    else:
        assert output.is_symlink()


def test_batch_task_cannot_collide_with_index(tmp_path, batch_package, no_docker):
    selected = select_preload_inputs(batch_package)[:2]
    selected[1] = selected[1].model_copy(update={"task_id": "preloads.json"})
    with pytest.raises(RecordUsageError, match="index"):
        preload_output_paths(batch_package, selected, tmp_path / "batch")
    assert not (tmp_path / "batch").exists()
