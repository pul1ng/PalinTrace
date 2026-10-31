import json
import sys
import types

import pytest

from palinreplay.tasks import Draft, FetchRecord, SourceSpec, download_dataset, load_fetch, register
from palinreplay.tasks.model import DatasetSource, EnvironmentSpec, ImageSpec
from palinreplay.tasks.pipeline import read_rows


def fake_hf(monkeypatch, rows, *, revision="d" * 40):
    module, hub = types.ModuleType("datasets"), types.ModuleType("huggingface_hub")
    calls = []

    class Rows(list):
        _fingerprint = "fingerprint-1"

    class HfApi:
        def dataset_info(self, dataset, revision=None):
            calls.append(("resolve", dataset, revision))
            return types.SimpleNamespace(sha=(revision and "e" * 40) or "d" * 40)

    def load(dataset, **kwargs):
        calls.append(("load", dataset, kwargs))
        return Rows(rows)

    module.load_dataset = load
    hub.HfApi = HfApi
    monkeypatch.setitem(sys.modules, "datasets", module)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    return calls


def test_download_writes_rows_and_provenance(tmp_path, row, monkeypatch):
    calls = fake_hf(monkeypatch, [row, row | {"instance_id": "example__repo-2"}])
    record = download_dataset(tmp_path / "raw", source="swebench_lite")
    assert record.row_count == 2
    assert record.source == "swebench_lite"
    assert record.dataset == "princeton-nlp/SWE-bench_Lite"
    assert record.split == "test"
    assert record.revision == "d" * 40
    assert calls[0] == ("resolve", "princeton-nlp/SWE-bench_Lite", None)

    rows = [json.loads(line) for line in (tmp_path / "raw" / "rows.jsonl").read_text().splitlines()]
    assert [r["instance_id"] for r in rows] == ["example__repo-1", "example__repo-2"]
    # original fields survive untouched, including evaluation-only columns
    assert rows[0]["patch"] == row["patch"]
    assert rows[0]["unknown_upstream_field"] == {"preserve": True}

    assert load_fetch(tmp_path / "raw") == record
    assert record.as_dataset_source().input_sha256 == record.rows_sha256


def test_download_limit_and_order(tmp_path, row, monkeypatch):
    rows = [row | {"instance_id": f"example__repo-{i}"} for i in range(1, 5)]
    fake_hf(monkeypatch, rows)
    record = download_dataset(tmp_path / "raw", source="swebench_lite", limit=2)
    assert record.row_count == 2
    ids = [
        json.loads(line)["instance_id"]
        for line in (tmp_path / "raw" / "rows.jsonl").read_text().splitlines()
    ]
    assert ids == ["example__repo-1", "example__repo-2"]


def test_download_revision_pin_is_resolved_not_echoed(tmp_path, row, monkeypatch):
    calls = fake_hf(monkeypatch, [row])
    record = download_dataset(tmp_path / "raw", source="swebench_lite", revision="main")
    assert calls[0] == ("resolve", "princeton-nlp/SWE-bench_Lite", "main")
    assert record.revision == "e" * 40


def test_download_refuses_existing_output(tmp_path, row, monkeypatch):
    fake_hf(monkeypatch, [row])
    output = tmp_path / "raw"
    output.mkdir()
    marker = output / "keep"
    marker.write_text("x")
    with pytest.raises(FileExistsError):
        download_dataset(output, source="swebench_lite")
    assert marker.read_text() == "x"


def test_download_leaves_nothing_on_failure(tmp_path, row, monkeypatch):
    fake_hf(monkeypatch, [row, row | {"instance_id": "bad"}])

    # a row that cannot be serialized aborts before publishing
    def blow_up(*args, **kwargs):
        return [row, {"nested": {1, 2}}]  # a set is not JSON serializable

    adapter = register(
        SourceSpec(
            name="toy_bad_rows",
            row_id=lambda r: "x",
            build=lambda r, o: Draft(
                instruction="i",
                environment=EnvironmentSpec(
                    image=ImageSpec(reference="e/t:1", platform="linux/amd64", source="convention")
                ),
            ),
            fetch=lambda dataset, split, revision: (
                blow_up(),
                DatasetSource(source="toy_bad_rows", dataset="x/y", split=split),
            ),
            dataset="x/y",
        )
    )
    with pytest.raises(ValueError, match="not JSON serializable"):
        download_dataset(tmp_path / "raw", source=adapter.name)
    assert not (tmp_path / "raw").exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_download_rejects_bad_limit(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(
        sys.modules.get("datasets", types.ModuleType("datasets")),
        "load_dataset",
        None,
        raising=False,
    )
    with pytest.raises(ValueError, match="limit"):
        download_dataset(tmp_path / "raw", source="swebench_lite", limit=limit)
    assert not (tmp_path / "raw").exists()


def test_download_needs_a_dataset_on_the_adapter(tmp_path):
    adapter = register(
        SourceSpec(
            name="toy_no_dataset",
            row_id=lambda r: "x",
            build=lambda r, o: Draft(
                instruction="i",
                environment=EnvironmentSpec(
                    image=ImageSpec(reference="e/t:1", platform="linux/amd64", source="convention")
                ),
            ),
        )
    )
    with pytest.raises(ValueError, match="declares no dataset"):
        download_dataset(tmp_path / "raw", source=adapter.name)


def test_download_rejects_empty_result(tmp_path, monkeypatch):
    fake_hf(monkeypatch, [])
    with pytest.raises(ValueError, match="no rows"):
        download_dataset(tmp_path / "raw", source="swebench_lite")
    assert not (tmp_path / "raw").exists()


def test_missing_datasets_extra_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "datasets", None)
    with pytest.raises(RuntimeError, match=r"palinreplay\[swebench\]"):
        download_dataset(tmp_path / "raw", source="swebench_lite")


def test_load_fetch_detects_tampering(tmp_path, row, monkeypatch):
    fake_hf(monkeypatch, [row])
    download_dataset(tmp_path / "raw", source="swebench_lite")
    (tmp_path / "raw" / "rows.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="does not match"):
        load_fetch(tmp_path / "raw")


def test_fetch_record_rejects_foreign_rows_file():
    with pytest.raises(ValueError):
        FetchRecord(
            source="swebench_lite",
            dataset="princeton-nlp/SWE-bench_Lite",
            row_count=1,
            rows_file="../escape.jsonl",
            rows_sha256="a" * 64,
        )


def test_convert_reads_only_downloaded_rows(tmp_path, row, monkeypatch):
    """A download directory also holds fetch.json; convert must not treat it as data."""
    from palinreplay.tasks import convert, load_dataset

    fake_hf(monkeypatch, [row])
    raw = tmp_path / "raw"
    download_dataset(raw, source="swebench_lite")
    manifest = convert(tmp_path / "tasks", source="swebench_lite", input_path=raw)
    assert len(manifest.tasks) == 1
    _, tasks = load_dataset(tmp_path / "tasks")
    assert tasks[0].spec.task_id == row["instance_id"]


def test_convert_inherits_fetch_provenance(tmp_path, row, monkeypatch):
    from palinreplay.tasks import convert

    fake_hf(monkeypatch, [row])
    raw = tmp_path / "raw"
    record = download_dataset(raw, source="swebench_lite")
    manifest = convert(tmp_path / "tasks", input_path=raw)
    assert manifest.source == record.as_dataset_source()


def test_parquet_format_writes_parquet_and_reads_back(tmp_path, row, monkeypatch):
    pytest.importorskip("pyarrow")
    fake_hf(monkeypatch, [row, row | {"instance_id": "example__repo-2"}])
    record = download_dataset(tmp_path / "raw", source="swebench_lite", format="parquet")
    assert record.format == "parquet"
    assert record.rows_file == "rows.parquet"
    assert (tmp_path / "raw" / "rows.parquet").is_file()
    assert not (tmp_path / "raw" / "rows.jsonl").exists()
    rows = read_rows(tmp_path / "raw", label="swebench_lite")
    assert [r["instance_id"] for r in rows] == ["example__repo-1", "example__repo-2"]
    assert load_fetch(tmp_path / "raw") == record


def test_parquet_download_converts(tmp_path, row, monkeypatch):
    pytest.importorskip("pyarrow")
    from palinreplay.tasks import convert, load_dataset

    fake_hf(monkeypatch, [row])
    raw = tmp_path / "raw"
    download_dataset(raw, source="swebench_lite", format="parquet")
    manifest = convert(tmp_path / "tasks", source="swebench_lite", input_path=raw)
    assert len(manifest.tasks) == 1
    assert load_dataset(tmp_path / "tasks")[1][0].instruction == row["problem_statement"]


def test_parquet_without_pyarrow_is_actionable(tmp_path, row, monkeypatch):
    fake_hf(monkeypatch, [row])
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    with pytest.raises(RuntimeError, match=r"palinreplay\[parquet\]"):
        download_dataset(tmp_path / "raw", source="swebench_lite", format="parquet")
    assert not (tmp_path / "raw").exists()


def test_bad_format_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys.modules.get("datasets", types.ModuleType("datasets")),
        "load_dataset",
        None,
        raising=False,
    )
    with pytest.raises(ValueError, match="format must be one of"):
        download_dataset(tmp_path / "raw", source="swebench_lite", format="csv")
    assert not (tmp_path / "raw").exists()


def test_old_jsonl_record_without_format_field_still_loads(tmp_path, row, monkeypatch):
    """Backward compatibility: earlier fetch.json had no `format` key."""
    import json

    fake_hf(monkeypatch, [row])
    raw = tmp_path / "raw"
    download_dataset(raw, source="swebench_lite")
    payload = json.loads((raw / "fetch.json").read_text())
    payload.pop("format")
    (raw / "fetch.json").write_text(json.dumps(payload))
    record = load_fetch(raw)
    assert record.format == "jsonl"
    assert record.rows_file == "rows.jsonl"


def test_fetch_record_rejects_mismatched_rows_file():
    with pytest.raises(ValueError, match="rows_file must be"):
        FetchRecord(
            source="swebench_lite",
            dataset="princeton-nlp/SWE-bench_Lite",
            row_count=1,
            format="parquet",
            rows_file="rows.jsonl",
            rows_sha256="a" * 64,
        )
