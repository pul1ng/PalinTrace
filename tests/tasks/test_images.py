import json
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from palinreplay.cli import main
from palinreplay.tasks import convert_rows, images
from palinreplay.tasks.model import DatasetSource, file_sha256, load_dataset
from palinreplay.tasks.sources import resolve

IMAGE_ID = "sha256:" + "a" * 64
REPO_DIGEST = "registry.example/task@sha256:" + "b" * 64


def dataset(tmp_path, row, *, two=False):
    output = tmp_path / "dataset"
    rows = [row | {"image_name": "registry.example/task:v1"}]
    if two:
        rows.append(
            row | {"instance_id": "example__repo-2", "image_name": "registry.example/task:v1"}
        )
    convert_rows(
        rows,
        output,
        adapter=resolve("swebench_lite"),
        source=DatasetSource(source="swebench_lite", dataset="unit/test"),
    )
    return output


def docker_mock(monkeypatch, *, architecture="amd64", failure=None):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[0] == "docker"
        assert kwargs["check"]
        if not kwargs["capture_output"]:
            assert command[1] == "pull"
            assert kwargs["stdout"] is kwargs["stderr"]
        assert not kwargs.get("shell")
        if failure and command[1] == failure:
            raise subprocess.CalledProcessError(1, command, stderr="mock failure")
        if command[1:3] == ["image", "inspect"]:
            stdout = json.dumps(
                [
                    {
                        "Id": IMAGE_ID,
                        "RepoDigests": [REPO_DIGEST],
                        "Os": "linux",
                        "Architecture": architecture,
                    }
                ]
            )
        elif command[1:3] == ["image", "save"]:
            Path(command[command.index("--output") + 1]).write_bytes(b"mock image archive")
            stdout = ""
        else:
            assert command[1] == "pull"
            stdout = "pulled"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(images.subprocess, "run", run)
    return calls


def test_pull_inspect_dedup_and_archive_without_mutating_tasks(tmp_path, row, monkeypatch):
    output = dataset(tmp_path, row, two=True)
    old_manifest = (output / "manifest.json").read_bytes()
    calls = docker_mock(monkeypatch)
    result = images.prepare_images(output, archive_dir=tmp_path / "archives")
    assert len(result.images) == 1
    image = result.images[0]
    assert image.image_id == IMAGE_ID
    assert image.repo_digests == [REPO_DIGEST]
    assert image.task_ids == [row["instance_id"], "example__repo-2"]
    assert calls[0] == ["docker", "pull", "--platform", "linux/amd64", "registry.example/task:v1"]
    assert calls[2][-1] == IMAGE_ID
    assert len(calls) == 3
    assert image.archive_sha256 == file_sha256(Path(image.archive_path))
    assert (output / "manifest.json").read_bytes() == old_manifest
    _, tasks = load_dataset(output)
    assert tasks[0].spec.environment.image.status == "unresolved"
    assert json.loads((output / "images.json").read_text())["images"][0]["image_id"] == IMAGE_ID


@pytest.mark.parametrize("archive", [False, True])
def test_download_mirror_cli_uses_existing_preparation(tmp_path, row, monkeypatch, capsys, archive):
    output = dataset(tmp_path, row, two=True)
    originals = {p: p.read_bytes() for p in output.rglob("*") if p.is_file()}
    calls = docker_mock(monkeypatch)
    argv = ["download", "mirror", str(output)]
    if archive:
        argv += ["--archive-dir", str(tmp_path / "archives")]
    assert main(argv) == 0
    displayed = capsys.readouterr()
    assert f"Prepared 1 unique images; wrote {output}/images.json" in displayed.out
    assert "镜像就绪 [------------------------] 0/1" in displayed.err
    assert "镜像就绪 [========================] 1/1" in displayed.err
    assert "下载镜像 1/1" in displayed.err and "核验镜像" in displayed.err
    assert ("归档镜像" in displayed.err) is archive
    result = images.PreparedImages.model_validate_json((output / "images.json").read_text())
    assert result.dataset_manifest_sha256 == file_sha256(output / "manifest.json")
    assert len(result.images) == 1
    prepared = result.images[0]
    assert prepared.image_id == IMAGE_ID
    assert prepared.repo_digests == [REPO_DIGEST]
    assert prepared.task_ids == [row["instance_id"], "example__repo-2"]
    assert len(calls) == (3 if archive else 2)
    assert calls[0] == ["docker", "pull", "--platform", "linux/amd64", "registry.example/task:v1"]
    assert calls[1] == ["docker", "image", "inspect", "registry.example/task:v1"]
    if archive:
        assert prepared.archive_sha256 == file_sha256(Path(prepared.archive_path))
        assert calls[2][1:3] == ["image", "save"]
        assert calls[2][-1] == IMAGE_ID
    else:
        assert prepared.archive_path is None
        assert prepared.archive_sha256 is None
    assert all(p.read_bytes() == content for p, content in originals.items())


def test_wrong_platform_is_not_reported_as_prepared(tmp_path, row, monkeypatch):
    output = dataset(tmp_path, row)
    docker_mock(monkeypatch, architecture="arm64")
    with pytest.raises(ValueError, match="platform mismatch"):
        images.prepare_images(output)
    assert not (output / "images.json").exists()


def test_pull_failure_does_not_publish_manifest(tmp_path, row, monkeypatch):
    output = dataset(tmp_path, row)
    docker_mock(monkeypatch, failure="pull")
    with pytest.raises(RuntimeError, match="mock failure"):
        images.prepare_images(output)
    assert not (output / "images.json").exists()


def test_missing_docker_has_clear_error(tmp_path, row, monkeypatch):
    output = dataset(tmp_path, row)

    def missing(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(images.subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="Docker CLI"):
        images.prepare_images(output)
    assert not (output / "images.json").exists()


def test_existing_manifest_is_not_overwritten(tmp_path, row, monkeypatch):
    output = dataset(tmp_path, row)
    (output / "images.json").write_text("existing")
    monkeypatch.setattr(
        images.subprocess, "run", lambda *a, **k: pytest.fail("must not run Docker")
    )
    with pytest.raises(FileExistsError):
        images.prepare_images(output)
    assert (output / "images.json").read_text() == "existing"


def test_corruption_detected_before_docker(tmp_path, row, monkeypatch):
    output = dataset(tmp_path, row)
    (output / row["instance_id"] / "instruction.md").write_text("corrupt")
    monkeypatch.setattr(
        images.subprocess, "run", lambda *a, **k: pytest.fail("must not run Docker")
    )
    with pytest.raises(ValueError, match="corrupt"):
        images.prepare_images(output)


def test_archive_failure_is_not_published(tmp_path, row, monkeypatch):
    output = dataset(tmp_path, row)
    original = docker_mock(monkeypatch)
    run = images.subprocess.run

    def fail_save(command, **kwargs):
        if command[1:3] == ["image", "save"]:
            raise subprocess.CalledProcessError(1, command, stderr="save failed")
        return run(command, **kwargs)

    monkeypatch.setattr(images.subprocess, "run", fail_save)
    with pytest.raises(RuntimeError, match="save failed"):
        images.prepare_images(output, archive_dir=tmp_path / "archives")
    assert len(original) == 2
    assert not (output / "images.json").exists()
    assert list((tmp_path / "archives").iterdir()) == []


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_rejected_before_any_docker_call(tmp_path, monkeypatch, timeout):
    monkeypatch.setattr(images.subprocess, "run", lambda *a, **k: pytest.fail("no Docker"))
    with pytest.raises(ValueError, match="finite"):
        images.prepare_images(tmp_path / "missing", timeout=timeout)


def test_two_references_to_same_image_share_export(tmp_path, row, monkeypatch):
    output = tmp_path / "dataset"
    convert_rows(
        [
            row | {"image_name": "registry.example/task:v1"},
            row | {"instance_id": "example__repo-2", "image_name": "registry.example/task:v2"},
        ],
        output,
        adapter=resolve("swebench_lite"),
        source=DatasetSource(source="swebench_lite", dataset="unit/test"),
    )
    calls = docker_mock(monkeypatch)
    result = images.prepare_images(output, archive_dir=tmp_path / "archives")
    assert len(result.images) == 2
    assert len([call for call in calls if call[1:3] == ["image", "save"]]) == 1
    assert result.images[0].archive_path == result.images[1].archive_path


def test_library_preparation_remains_quiet_by_default(tmp_path, row, monkeypatch, capsys):
    output = dataset(tmp_path, row)
    docker_mock(monkeypatch)
    images.prepare_images(output)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_progress_does_not_count_an_unverified_image(tmp_path, row, monkeypatch, capsys):
    output = dataset(tmp_path, row)
    docker_mock(monkeypatch, architecture="arm64")
    with pytest.raises(ValueError, match="platform mismatch"):
        images.prepare_images(output, show_progress=True)
    captured = capsys.readouterr()
    assert "0/1" in captured.err and "核验镜像" in captured.err
    assert "[========================] 1/1" not in captured.err
    assert not (output / "images.json").exists()


def test_docker_pull_output_is_visible_before_process_completion(tmp_path, monkeypatch):
    gate, log = tmp_path / "release", tmp_path / "progress.log"
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "printf 'Downloading [====>    ] 50%%\\n'\n"
        f"while [ ! -f {shlex.quote(str(gate))} ]; do sleep 0.01; done\n"
        "printf 'Pull complete\\n' >&2\n"
    )
    docker.chmod(0o700)
    real_run = subprocess.run
    monkeypatch.setattr(
        images.subprocess, "run", lambda command, **kw: real_run([str(docker), *command[1:]], **kw),
    )
    results, errors = [], []

    def pull():
        try:
            results.append(images._docker(["pull", "fixture"], 10, stream_output=True))
        except BaseException as error:
            errors.append(error)

    with log.open("w") as stream:
        monkeypatch.setattr(sys, "stderr", stream)
        worker = threading.Thread(target=pull)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while "Downloading" not in log.read_text() and worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert "Downloading [====>    ] 50%" in log.read_text()
            assert worker.is_alive()  # Output is already visible while the child waits.
        finally:
            gate.touch()
            worker.join(timeout=5)
        assert not worker.is_alive()
    assert not errors and results == [""]
    assert "Pull complete" in log.read_text()


def test_streamed_pull_failure_keeps_diagnostics_and_does_not_publish(tmp_path, row, monkeypatch):
    output = dataset(tmp_path, row)
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nprintf 'registry unavailable\\n' >&2\nexit 7\n")
    docker.chmod(0o700)
    real_run = subprocess.run
    monkeypatch.setattr(
        images.subprocess, "run", lambda command, **kw: real_run([str(docker), *command[1:]], **kw),
    )
    log = tmp_path / "progress.log"
    with log.open("w") as stream:
        monkeypatch.setattr(sys, "stderr", stream)
        with pytest.raises(RuntimeError, match="exit code 7"):
            images.prepare_images(output, show_progress=True)
    assert "registry unavailable" in log.read_text()
    assert "[========================] 1/1" not in log.read_text()
    assert not (output / "images.json").exists()
