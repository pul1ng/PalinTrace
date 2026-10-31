import hashlib
import json
import sys
import types

import pytest

from palinreplay.tasks import (
    BuildOptions,
    DatasetSource,
    Draft,
    SourceSpec,
    convert,
    convert_rows,
    load_dataset,
    load_task,
    read_rows,
    register,
)
from palinreplay.tasks.model import Artifact, EnvironmentSpec, ImageSpec
from palinreplay.tasks.sources import available, resolve
from palinreplay.tasks.sources.swebench import common as sb


def test_local_roundtrip_and_private_evaluation_data(tmp_path, row):
    source = tmp_path / "input.json"
    source.write_text(json.dumps([row]), encoding="utf-8")
    out = tmp_path / "converted"
    manifest = convert(out, source="swebench_lite", input_path=source)
    loaded_manifest, tasks = load_dataset(out)
    assert loaded_manifest == manifest
    task = tasks[0]
    assert task.instruction == row["problem_statement"]
    assert task.evaluation_artifacts()["evaluation/source.json"] == row
    assert task.spec.repository.base_commit == row["base_commit"]
    assert task.spec.source.input_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert task.spec.environment.workdir == "/testbed"
    assert task.spec.environment.env["BASH_ENV"] == "/root/.bashrc"
    assert task.spec.environment.timeout_sec == 60
    agent_input = json.dumps(task.agent_input())
    for private in [row["patch"], row["test_patch"], row["hints_text"]]:
        assert private not in agent_input
    assert not (out / "images.json").exists()


@pytest.mark.parametrize("extension", [".json", ".jsonl", ".ndjson"])
def test_input_shapes(tmp_path, row, extension):
    path = tmp_path / f"input{extension}"
    path.write_text(json.dumps([row]) if extension == ".json" else json.dumps(row) + "\n\n")
    assert read_rows(path, label="swebench") == [row]


def test_single_object_and_utf8_bom(tmp_path, row):
    path = tmp_path / "input.json"
    path.write_text("\ufeff" + json.dumps(row), encoding="utf-8")
    assert read_rows(path, label="swebench") == [row]


def test_one_source_per_swebench_variant():
    names = [adapter.name for adapter in available()]
    assert names == sorted(set(names))
    assert {"swebench_full", "swebench_lite", "swebench_verified"} <= set(names)
    assert resolve("swe-bench") is resolve("swebench_full")
    datasets = {a.name: a.dataset for a in available() if a.name.startswith("swebench")}
    assert datasets == {
        "swebench_full": "princeton-nlp/SWE-bench",
        "swebench_lite": "princeton-nlp/SWE-bench_Lite",
        "swebench_verified": "princeton-nlp/SWE-bench_Verified",
    }


def test_variant_rejects_another_variant_dataset():
    with pytest.raises(ValueError, match="targets 'princeton-nlp/SWE-bench_Lite'"):
        sb.fetch("verified", "test", None, variant="lite")


def test_variant_evaluation_kind_records_variant(row):
    for variant in ("full", "lite", "verified"):
        draft = sb.build(row, BuildOptions(), variant=variant)
        assert draft.evaluation_kind == f"swebench-{variant}-source"


def test_unknown_source_lists_available():
    with pytest.raises(ValueError, match="unknown task source"):
        convert("out", source="nope", input_path="input.json")


def test_public_image_naming_and_arch():
    image = sb.resolve_image({"instance_id": "django__django-11099"}, BuildOptions())
    assert image.reference == "docker.io/swebench/sweb.eval.x86_64.django_1776_django-11099:latest"
    assert image.platform == "linux/amd64"
    assert image.status == "unresolved"
    arm = sb.resolve_image({"instance_id": "example__repo-1"}, BuildOptions(arch="arm64"))
    assert arm.platform == "linux/arm64"
    assert "sweb.eval.arm64." in arm.reference


def test_image_priority_and_digest_preservation(row):
    digest = "registry.example/team/task@sha256:" + "b" * 64
    data = row | {"image_name": digest, "docker_image": "other/task:tag"}
    assert sb.resolve_image(data, BuildOptions()).source == "image_name"
    assert sb.resolve_image(row | {"docker_image": "other/task:tag"}, BuildOptions()).source == (
        "docker_image"
    )
    mapping = BuildOptions(image_map={row["instance_id"]: "custom/image:v1"})
    assert sb.resolve_image(data, mapping).reference == "custom/image:v1"


@pytest.mark.parametrize(
    "reference",
    ["--privileged", "https://registry.example/image", "image; touch /tmp/x", "image\nother"],
)
def test_invalid_image_references(row, reference):
    with pytest.raises(ValueError):
        sb.resolve_image(row | {"image_name": reference}, BuildOptions())


def test_selection_preserves_requested_order_then_applies_limit(tmp_path, row):
    second = row | {"instance_id": "example__repo-2"}
    manifest = convert_rows(
        [row, second],
        tmp_path / "out",
        adapter=resolve("swebench_lite"),
        source=DatasetSource(source="swebench_lite", dataset="unit/test"),
        ids=[second["instance_id"], row["instance_id"]],
        limit=1,
    )
    assert [entry.task_id for entry in manifest.tasks] == [second["instance_id"]]


@pytest.mark.parametrize(
    "options",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": 1.5},
        {"limit": True},
        {"ids": ["missing"]},
        {"ids": ["example__repo-1", "example__repo-1"]},
        {"ids": []},
    ],
)
def test_invalid_selection_does_not_publish(tmp_path, row, options):
    output = tmp_path / "out"
    with pytest.raises(ValueError):
        convert_rows(
            [row],
            output,
            adapter=resolve("swebench_lite"),
            source=DatasetSource(source="swebench_lite", dataset="unit/test"),
            **options,
        )
    assert not output.exists()


def test_duplicates_and_late_invalid_rows_are_rejected(tmp_path, row):
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="duplicate"):
        convert_rows(
            [row, row],
            output,
            adapter=resolve("swebench_lite"),
            source=DatasetSource(source="swebench_lite", dataset="unit/test"),
        )
    bad = row | {"instance_id": "example__repo-2", "repo": ""}
    with pytest.raises(ValueError, match="repo"):
        convert_rows(
            [row, bad],
            output,
            adapter=resolve("swebench_lite"),
            source=DatasetSource(source="swebench_lite", dataset="unit/test"),
        )
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("task_id", ["../escape", "/absolute", "a/b", "a\\b", ".", ".."])
def test_task_ids_cannot_escape_output(tmp_path, row, task_id):
    with pytest.raises(ValueError):
        convert_rows(
            [row | {"instance_id": task_id}],
            tmp_path / "out",
            adapter=resolve("swebench_lite"),
            source=DatasetSource(source="swebench_lite", dataset="unit/test"),
        )
    assert not (tmp_path / "out").exists()


def test_existing_output_is_never_overwritten(tmp_path, row):
    output = tmp_path / "out"
    output.mkdir()
    marker = output / "keep"
    marker.write_text("untouched")
    with pytest.raises(FileExistsError):
        convert(output, source="swebench_lite", input_path=tmp_path / "unused.json")
    assert marker.read_text() == "untouched"


@pytest.mark.parametrize("artifact", ["instruction.md", "evaluation/source.json", "task.json"])
def test_loader_detects_tampered_artifacts(tmp_path, row, artifact):
    output = tmp_path / "out"
    convert(output, source="swebench_lite", input_path=_write(tmp_path, row))
    (
        output / (row["instance_id"] + "/" + artifact)
        if artifact != "manifest.json"
        else output / artifact
    ).write_text("tampered")
    with pytest.raises(ValueError, match="corrupt"):
        load_dataset(output)


def test_loader_rejects_artifact_symlink_escape(tmp_path, row):
    output = tmp_path / "out"
    convert(output, source="swebench_lite", input_path=_write(tmp_path, row))
    task_dir = output / row["instance_id"]
    outside = tmp_path / "outside.txt"
    outside.write_text(row["problem_statement"])
    (task_dir / "instruction.md").unlink()
    (task_dir / "instruction.md").symlink_to(outside)
    with pytest.raises(ValueError, match="outside"):
        load_task(task_dir)


@pytest.mark.parametrize("path", ["../x", "/tmp/x", "a\\b", "a\x00b"])
def test_invalid_artifact_paths(path):
    with pytest.raises(ValueError):
        Artifact(path=path, sha256="a" * 64)


def test_malformed_jsonl_reports_line(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"instance_id":"ok"}\n{broken}\n')
    with pytest.raises(ValueError, match="line 2"):
        read_rows(path, label="swebench")


@pytest.mark.parametrize("payload", [[1, 2], "text", 5])
def test_invalid_input_shape(tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="rows of objects"):
        read_rows(path, label="swebench")


def test_directory_input_reads_sorted_shards(tmp_path, row):
    data = tmp_path / "data"
    data.mkdir()
    first = row | {"instance_id": "example__repo-1"}
    second = row | {"instance_id": "example__repo-2"}
    (data / "test-00001-of-00002.jsonl").write_text(json.dumps(second) + "\n")
    (data / "test-00000-of-00002.jsonl").write_text(json.dumps(first) + "\n")
    (data / "README.md").write_text("ignored")
    assert [r["instance_id"] for r in read_rows(data, label="swebench")] == [
        "example__repo-1",
        "example__repo-2",
    ]
    manifest = convert(tmp_path / "out", source="swebench_lite", input_path=data)
    assert len(manifest.tasks) == 2
    assert manifest.source.input_sha256


def test_directory_without_data_files_is_rejected(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no readable data files"):
        read_rows(empty, label="swebench")


def test_parquet_dispatch_uses_pyarrow(monkeypatch, tmp_path):
    calls = []

    class Table:
        def to_pylist(self):
            return [{"instance_id": "example__repo-1"}]

    module = types.ModuleType("pyarrow.parquet")
    module.read_table = lambda path: calls.append(path) or Table()
    package = types.ModuleType("pyarrow")
    package.parquet = module
    monkeypatch.setitem(sys.modules, "pyarrow", package)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", module)
    path = tmp_path / "test-00000-of-00001.parquet"
    path.write_bytes(b"not really parquet; the reader is mocked")
    assert read_rows(path, label="swebench") == [{"instance_id": "example__repo-1"}]
    assert calls == [path]


def test_missing_pyarrow_has_actionable_error(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    path = tmp_path / "rows.parquet"
    path.write_bytes(b"")
    with pytest.raises(RuntimeError, match=r"palinreplay\[parquet\]"):
        read_rows(path, label="swebench")


def test_real_parquet_roundtrip(tmp_path, row):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    path = tmp_path / "test-00000-of-00001.parquet"
    pq.write_table(pa.Table.from_pylist([row]), path)
    assert read_rows(path, label="swebench") == [row]


def test_rows_missing_required_fields_fail_before_publishing(tmp_path):
    path = tmp_path / "rows.json"
    path.write_text(json.dumps([{"unexpected": []}]))
    with pytest.raises(ValueError, match="instance_id"):
        convert(tmp_path / "out", source="swebench_lite", input_path=path)
    assert not (tmp_path / "out").exists()


def test_hugging_face_resolves_revision_before_loading(monkeypatch, row):
    calls = []
    module, hub = types.ModuleType("datasets"), types.ModuleType("huggingface_hub")

    class Rows(list):
        _fingerprint = "fingerprint-1"

    class HfApi:
        def dataset_info(self, dataset, revision):
            calls.append(("resolve", dataset, revision))
            return types.SimpleNamespace(sha="d" * 40)

    def load(dataset, **kwargs):
        calls.append(("load", dataset, kwargs))
        return Rows([row])

    module.load_dataset = load
    hub.HfApi = HfApi
    monkeypatch.setitem(sys.modules, "datasets", module)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    records, source = sb.fetch("lite", "test", "main", variant="lite")
    assert records == [row]
    assert calls[0] == ("resolve", "princeton-nlp/SWE-bench_Lite", "main")
    assert calls[1][2] == {"split": "test", "revision": "d" * 40}
    assert (source.source, source.revision, source.fingerprint) == (
        "swebench_lite",
        "d" * 40,
        "fingerprint-1",
    )


def test_optional_hf_dependency_error_is_actionable(monkeypatch):
    monkeypatch.setitem(sys.modules, "datasets", None)
    with pytest.raises(RuntimeError, match=r"palinreplay\[swebench\]"):
        sb.fetch("lite", "test", None, variant="lite")


def test_convert_requires_local_input_and_rejects_online_options(tmp_path):
    with pytest.raises(TypeError, match="input_path"):
        convert(tmp_path / "out", source="swebench_lite")
    for remote_options in ({"online": True}, {"dataset": "lite"}):
        with pytest.raises(TypeError, match="unexpected keyword"):
            convert(tmp_path / "out", source="swebench_lite", input_path="a.json", **remote_options)
    assert not (tmp_path / "out").exists()


def test_local_conversion_never_fetches(tmp_path, monkeypatch, row):
    def forbidden(*args, **kwargs):
        raise AssertionError("convert must not fetch remote data")

    monkeypatch.setattr(sb, "fetch", forbidden)
    manifest = convert(tmp_path / "out", source="swebench_lite", input_path=_write(tmp_path, row))
    assert manifest.source.dataset == "local/swebench_lite"
    assert manifest.source.source == "swebench_lite"


def test_local_source_provenance(tmp_path, row):
    path = tmp_path / "source.json"
    path.write_text(json.dumps([row]))
    manifest = convert(
        tmp_path / "out", source="swebench_lite", input_path=path, revision="exported-revision"
    )
    assert manifest.source.dataset == "local/swebench_lite"
    assert manifest.source.revision == "exported-revision"
    assert manifest.source.input_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_new_dataset_needs_only_an_adapter(tmp_path):
    """Extension check: one SourceSpec, no pipeline/CLI/storage change."""

    def row_id(raw):
        return raw["name"]

    def build(raw, options):
        return Draft(
            instruction=raw["question"],
            environment=EnvironmentSpec(
                image=ImageSpec(
                    reference="example/task:latest", platform="linux/amd64", source="convention"
                )
            ),
            evaluation_kind="toy-hidden",
            evaluation_files={"answer.json": json.dumps({"answer": raw["answer"]})},
        )

    adapter = register(SourceSpec(name="toy", aliases=("toy-bench",), row_id=row_id, build=build))
    manifest = convert_rows(
        [{"name": "toy-1", "question": "2+2?", "answer": 4}],
        tmp_path / "out",
        adapter=adapter,
        source=DatasetSource(source="toy", dataset="toy/rows"),
    )
    _, tasks = load_dataset(tmp_path / "out")
    assert manifest.source.source == "toy"
    assert tasks[0].instruction == "2+2?"
    assert tasks[0].evaluation_artifacts()["evaluation/answer.json"] == {"answer": 4}
    assert "4" not in json.dumps(tasks[0].agent_input()["instruction"])
    assert resolve("toy-bench") is adapter


def _write(tmp_path, row):
    path = tmp_path / "input.json"
    path.write_text(json.dumps([row]))
    return path
