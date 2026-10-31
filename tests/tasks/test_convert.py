"""Conversion of downloaded packages is offline and preserves verified provenance."""

import hashlib
import json

import pytest

from palinreplay.tasks import FetchRecord, convert, load_dataset, read_rows


@pytest.fixture(params=["jsonl", "parquet"])
def downloaded(tmp_path, row, request):
    root = tmp_path / "raw"
    root.mkdir()
    rows = [row, row | {"instance_id": "example__repo-2"}]
    format = request.param
    path = root / f"rows.{format}"
    if format == "parquet":
        pa = pytest.importorskip("pyarrow")
        pq = pytest.importorskip("pyarrow.parquet")
        pq.write_table(pa.Table.from_pylist(rows), path)
    else:
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    record = FetchRecord(
        source="swebench_lite",
        dataset="princeton-nlp/SWE-bench_Lite",
        split="dev",
        revision="a" * 40,
        fingerprint="test-fingerprint",
        row_count=2,
        format=format,
        rows_file=path.name,
        rows_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    (root / "fetch.json").write_text(record.model_dump_json())
    return root, record, rows


def test_infers_all_provenance_and_preserves_private_rows(tmp_path, downloaded, monkeypatch):
    from palinreplay.tasks.sources.swebench import common as sb

    def forbidden(*args, **kwargs):
        raise AssertionError("conversion must not fetch")

    monkeypatch.setattr(sb, "fetch", forbidden)
    root, record, rows = downloaded
    # Unrelated JSON data must not be included in conversion or the rows checksum.
    (root / "unrelated.json").write_text('{"not": "a task"}')
    manifest = convert(tmp_path / "out", input_path=root)
    assert manifest.source == record.as_dataset_source()
    loaded, tasks = load_dataset(tmp_path / "out")
    assert loaded == manifest
    for task, original in zip(tasks, rows, strict=True):
        assert task.spec.source == manifest.source
        assert task.evaluation_artifacts()["evaluation/source.json"] == original
        assert task.instruction == original["problem_statement"]
        assert task.spec.environment.image.status == "unresolved"
        agent = json.dumps(task.agent_input())
        for key in ("patch", "test_patch", "hints_text"):
            assert original[key] not in agent
    assert not (tmp_path / "out" / "images.json").exists()


def test_download_selection_keeps_original_provenance(tmp_path, downloaded):
    root, record, rows = downloaded
    manifest = convert(
        tmp_path / "out",
        input_path=root,
        ids=[rows[1]["instance_id"], rows[0]["instance_id"]],
        limit=1,
    )
    assert [task.task_id for task in manifest.tasks] == [rows[1]["instance_id"]]
    assert manifest.source == record.as_dataset_source()


@pytest.mark.parametrize(
    "options",
    [{"source": "swebench_verified"}, {"split": "test"}, {"revision": "another-revision"}],
)
def test_conflicting_overrides_are_rejected(tmp_path, downloaded, options):
    root, _, _ = downloaded
    with pytest.raises(ValueError, match=r"conflicts with fetch\.json"):
        convert(tmp_path / "out", input_path=root, **options)
    assert not (tmp_path / "out").exists()
    assert list(tmp_path.iterdir()) == [root]


def test_matching_overrides_are_accepted(tmp_path, downloaded):
    root, record, _ = downloaded
    manifest = convert(
        tmp_path / "out",
        input_path=root,
        source=record.source,
        split=record.split,
        revision=record.revision,
    )
    assert manifest.source == record.as_dataset_source()


def test_source_alias_matches_download_adapter(tmp_path, downloaded):
    root, record, _ = downloaded
    record = record.model_copy(
        update={"source": "swebench_full", "dataset": "princeton-nlp/SWE-bench"}
    )
    (root / "fetch.json").write_text(record.model_dump_json())
    manifest = convert(tmp_path / "out", input_path=root, source="swe-bench")
    assert manifest.source == record.as_dataset_source()


@pytest.mark.parametrize("damage", ["tamper", "missing", "row_count", "bad_record"])
def test_corrupt_download_never_falls_back_or_publishes(tmp_path, downloaded, damage):
    root, record, _ = downloaded
    if damage == "tamper":
        (root / record.rows_file).write_bytes(b"tampered")
    elif damage == "missing":
        (root / record.rows_file).unlink()
    elif damage == "row_count":
        (root / "fetch.json").write_text(
            record.model_copy(update={"row_count": 3}).model_dump_json()
        )
    else:
        (root / "fetch.json").write_text('{"broken":true}')
    # Even explicit --source cannot bypass the provenance checks.
    with pytest.raises(ValueError):
        convert(tmp_path / "out", input_path=root, source="swebench_lite")
    with pytest.raises(ValueError):
        read_rows(root, label="swebench_lite")
    assert not (tmp_path / "out").exists()
    assert list(tmp_path.iterdir()) == [root]


def test_bare_inputs_require_explicit_source(tmp_path, downloaded):
    root, record, _ = downloaded
    (root / "fetch.json").unlink()
    for path in (root, root / record.rows_file):
        with pytest.raises(ValueError, match="provide --source"):
            convert(tmp_path / "out", input_path=path)
        assert not (tmp_path / "out").exists()
    manifest = convert(tmp_path / "out", input_path=root, source="swebench_lite")
    assert manifest.source.dataset == "local/swebench_lite"
    assert manifest.source.revision is None


@pytest.mark.parametrize("selection", [{"limit": 1}, {"ids": ["example__repo-1"]}])
@pytest.mark.parametrize("field", ["instance_id", "repo", "base_commit", "problem_statement"])
def test_invalid_unselected_row_still_rejects_whole_input(tmp_path, row, selection, field):
    path = tmp_path / "rows.json"
    bad = row | {"instance_id": "example__repo-2", field: ""}
    path.write_text(json.dumps([row, bad]))
    with pytest.raises(ValueError, match=field):
        convert(tmp_path / "out", input_path=path, source="swebench_lite", **selection)
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("newline", ["\r\n", "\r", "\n"])
def test_instruction_preserves_original_newlines(tmp_path, row, newline):
    instruction = newline.join(["中文题面", "Keep original line endings.", ""])
    path = tmp_path / "rows.json"
    path.write_text(json.dumps([row | {"problem_statement": instruction}]))
    out = tmp_path / "out"
    convert(out, input_path=path, source="swebench_lite")
    _, tasks = load_dataset(out)
    task = tasks[0]
    assert (task.root / "instruction.md").read_bytes() == instruction.encode("utf-8")
    assert task.instruction == instruction
    assert task.agent_input()["instruction"] == instruction


def test_old_jsonl_fetch_converts_without_format_field(tmp_path, row):
    root = tmp_path / "raw"
    root.mkdir()
    path = root / "rows.jsonl"
    path.write_text(json.dumps(row) + "\n")
    record = FetchRecord(
        source="swebench_lite",
        dataset="princeton-nlp/SWE-bench_Lite",
        row_count=1,
        rows_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    data = record.model_dump()
    del data["format"]
    (root / "fetch.json").write_text(json.dumps(data))
    assert convert(tmp_path / "out", input_path=root).source == record.as_dataset_source()
