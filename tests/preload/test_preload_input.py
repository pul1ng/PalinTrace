import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from palinreplay.preload.input import PreloadInput, load_preload_input, load_preload_task
from palinreplay.tasks.images import PreparedImage, PreparedImages
from palinreplay.tasks.model import file_sha256
from tests.tasks.test_images import IMAGE_ID, REPO_DIGEST, dataset


def inputs(**overrides):
    return {
        "image": IMAGE_ID,
        "platform": "linux/amd64",
        "instruction": "Fix the example bug.",
    } | overrides


def write_input(tmp_path, data):
    path = tmp_path / "preload-input.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("image", [IMAGE_ID, REPO_DIGEST])
def test_standalone_input_preserves_text_and_explicit_configuration(tmp_path, monkeypatch, image):
    monkeypatch.setenv("PRELOAD_HOST_ONLY", "must not be inherited")
    instruction = "  修复示例问题。\r\nKeep whitespace.\r\n"
    source = inputs(
        image=image, instruction=instruction, env={"LANG": "C.UTF-8", "MULTILINE": "a\nb"},
        workdir="/testbed", shell=["bash", "-lc"], timeout_sec=90,
    )
    path = write_input(tmp_path, source)
    before = path.read_bytes()
    value = load_preload_input(path)
    assert value.image == image
    assert value.instruction == instruction
    assert value.instruction_sha256 == hashlib.sha256(instruction.encode()).hexdigest()
    assert value.env == source["env"]
    assert value.workdir == "/testbed" and value.shell == ("bash", "-lc")
    assert value.timeout_sec == 90
    assert path.read_bytes() == before
    assert "PRELOAD_HOST_ONLY" not in value.env
    assert PreloadInput.model_validate_json(value.model_dump_json()) == value


def test_defaults_are_independent_and_do_not_resolve_an_image(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not execute"))
    value = load_preload_input(write_input(tmp_path, inputs()))
    assert value.env == {}
    assert value.workdir == "/" and value.shell == ("bash", "-c")
    assert value.timeout_sec == 60
    assert value.task_id is None
    assert value.image_reference is None and value.repo_digests == ()
    value.env["ONLY_THIS_INPUT"] = "1"
    assert PreloadInput(**inputs()).env == {}


@pytest.mark.parametrize("image", [
    "registry.example/task:latest", "abc123", "sha256:" + "a" * 63,
    "sha256:" + "G" * 64, "repo@sha256:" + "a" * 63,
    "https://registry.example/repo@sha256:" + "a" * 64,
    "--privileged@sha256:" + "a" * 64, "repo@@sha256:" + "a" * 64,
])
def test_images_require_full_ids_or_repository_digests(image):
    with pytest.raises(ValidationError):
        PreloadInput(**inputs(image=image))


@pytest.mark.parametrize("changes", [
    {"instruction": ""}, {"instruction": " \r\n "}, {"instruction": 12},
    {"env": {"": "value"}}, {"env": {"BAD=KEY": "value"}},
    {"env": {"BAD\x00KEY": "value"}}, {"env": {"KEY": "bad\x00value"}},
    {"env": {"KEY": 1}},
    {"workdir": "relative"}, {"workdir": ""}, {"workdir": "/a/../b"},
    {"workdir": "/bad\x00path"},
    {"shell": []}, {"shell": [""]}, {"shell": ["bash\x00"]}, {"shell": "bash -c"},
    {"timeout_sec": 0}, {"timeout_sec": -1}, {"timeout_sec": float("inf")},
    {"timeout_sec": float("nan")}, {"timeout_sec": True},
    {"platform": "darwin/arm64"}, {"task_id": "../other"},
    {"schema_version": "palinreplay.preload-input.v2"}, {"unknown_option": True},
    {"repo_digests": [IMAGE_ID]},
])
def test_invalid_runtime_inputs_fail_before_execution(changes):
    with pytest.raises(ValidationError):
        PreloadInput(**inputs(**changes))


@pytest.mark.parametrize("missing", ["image", "platform", "instruction"])
def test_required_inputs_cannot_be_omitted(tmp_path, missing):
    source = inputs()
    del source[missing]
    with pytest.raises(ValidationError):
        load_preload_input(write_input(tmp_path, source))


@pytest.mark.parametrize("duplicate", [
    '"instruction":"first","instruction":"second"',
    '"env":{"LANG":"first","LANG":"second"}',
])
def test_duplicate_json_fields_are_not_silently_replaced(tmp_path, duplicate):
    path = tmp_path / "duplicate.json"
    path.write_text("{" + duplicate + "}")
    with pytest.raises(ValueError, match="duplicate JSON field"):
        load_preload_input(path)


def prepared_package(tmp_path, row, *, two=False):
    root = dataset(tmp_path, row, two=two)
    prepared = PreparedImages(
        dataset_manifest_sha256=file_sha256(root / "manifest.json"),
        images=[PreparedImage(
            reference="registry.example/task:v1", platform="linux/amd64", image_id=IMAGE_ID,
            repo_digests=[REPO_DIGEST],
            task_ids=[row["instance_id"], "example__repo-2"] if two else [row["instance_id"]],
        )],
    )
    (root / "images.json").write_text(prepared.model_dump_json())
    return root


def test_existing_task_adapter_is_read_only_and_excludes_evaluation(tmp_path, row, monkeypatch):
    row = row | {"problem_statement": "  修复示例。\r\nKeep CRLF.\r\n"}
    root = prepared_package(tmp_path, row)
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not execute"))
    value = load_preload_task(root)
    assert value.image == IMAGE_ID and value.platform == "linux/amd64"
    assert value.image_reference == "registry.example/task:v1"
    assert value.repo_digests == (REPO_DIGEST,)
    assert value.task_id == row["instance_id"]
    assert value.instruction == row["problem_statement"]
    assert value.workdir == "/testbed"
    assert value.env["BASH_ENV"] == "/root/.bashrc"
    text = value.model_dump_json()
    for private in ("synthetic gold patch", "synthetic hidden test patch", "private evaluation hint"):
        assert private not in text
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("damage", ["instruction", "images", "binding", "digest"])
def test_task_adapter_rejects_corrupt_or_unprepared_packages(tmp_path, row, damage):
    root = prepared_package(tmp_path, row)
    if damage == "instruction":
        (root / row["instance_id"] / "instruction.md").write_text("changed")
    elif damage == "images":
        (root / "images.json").unlink()
    else:
        path = root / "images.json"
        doc = json.loads(path.read_text())
        if damage == "binding":
            doc["dataset_manifest_sha256"] = "0" * 64
        else:
            doc["images"][0]["repo_digests"] = ["registry.example/task:latest"]
        path.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        load_preload_task(root)


def test_multi_task_input_requires_an_explicit_selection(tmp_path, row):
    root = prepared_package(tmp_path, row, two=True)
    with pytest.raises(ValueError, match="task_id is required"):
        load_preload_task(root)
    assert load_preload_task(root, task_id="example__repo-2").task_id == "example__repo-2"
    with pytest.raises(ValueError, match="unknown task ID"):
        load_preload_task(root, task_id="unknown")


def test_task_adapter_hashes_the_instruction_bytes_it_actually_reads(tmp_path, row, monkeypatch):
    root = prepared_package(tmp_path, row)
    instruction = root / row["instance_id"] / "instruction.md"
    read_bytes = Path.read_bytes

    def changed_read(path):
        return b"changed after verification" if path == instruction else read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", changed_read)
    with pytest.raises(ValueError, match="instruction changed"):
        load_preload_task(root)
