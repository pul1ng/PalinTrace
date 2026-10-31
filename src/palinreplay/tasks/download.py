"""Download raw dataset rows next to a provenance record; no conversion here."""

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from palinreplay.core.schema import Spec
from palinreplay.tasks.model import DatasetSource, json_text
from palinreplay.tasks.sources import resolve

FETCH_SCHEMA = "palinreplay.fetch.v1"
FETCH_FILE = "fetch.json"
FORMATS = ("jsonl", "parquet")


def rows_filename(fmt: str) -> str:
    return f"rows.{fmt}"


class FetchRecord(Spec):
    """What was downloaded, so a later ``convert`` can inherit real provenance."""

    schema_version: Literal["palinreplay.fetch.v1"] = FETCH_SCHEMA
    source: str = Field(min_length=1)
    dataset: str = Field(min_length=1)
    split: str = Field(default="test", min_length=1)
    revision: str | None = None
    fingerprint: str | None = None
    row_count: int = Field(ge=1)
    format: Literal["jsonl", "parquet"] = "jsonl"
    rows_file: str = "rows.jsonl"
    rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _rows_file_matches_format(self) -> "FetchRecord":
        expected = rows_filename(self.format)
        if self.rows_file != expected:
            raise ValueError(f"rows_file must be {expected!r} for format {self.format!r}")
        return self

    def as_dataset_source(self) -> DatasetSource:
        """Provenance view used by ``convert``; keeps the rows digest as input_sha256."""
        return DatasetSource(
            source=self.source,
            dataset=self.dataset,
            split=self.split,
            revision=self.revision,
            input_sha256=self.rows_sha256,
            fingerprint=self.fingerprint,
        )


def _write_jsonl(rows: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_canonical_line(row) + "\n")


def _write_parquet(rows: list[dict], path: Path) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError(
            '--format parquet requires: pip install "palinreplay[parquet]"'
        ) from exc
    try:
        table = pa.Table.from_pylist(rows)
        pq.write_table(table, path)
    except (pa.ArrowException, TypeError, ValueError) as exc:
        raise ValueError(f"could not encode rows as parquet: {exc}") from exc


def _canonical_line(row: dict) -> str:
    try:
        return json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"dataset row is not JSON serializable: {exc}") from exc


def download_dataset(
    output_dir: str | Path,
    *,
    source: str,
    split: str = "test",
    revision: str | None = None,
    limit: int | None = None,
    format: str = "jsonl",
) -> FetchRecord:
    """Fetch the source's own dataset and publish ``rows.jsonl`` + ``fetch.json``.

    Explicit network operation: this is the only place raw rows are acquired.
    No conversion, no Docker, no model calls. Output is staged and renamed once.
    """
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0):
        raise ValueError("limit must be a positive integer")
    if not split.strip():
        raise ValueError("split must not be blank")
    if format not in FORMATS:
        raise ValueError(f"format must be one of: {', '.join(FORMATS)}")
    adapter = resolve(source)
    if adapter.dataset is None:
        raise ValueError(f"source {adapter.name} declares no dataset; nothing to download")
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")

    rows, provenance = adapter.fetch(adapter.dataset, split, revision)
    selected = list(rows)
    if not selected:
        raise ValueError(f"dataset {provenance.dataset} returned no rows for split {split!r}")
    if limit is not None:
        selected = selected[:limit]

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}-", dir=output.parent) as temporary:
        stage = Path(temporary) / "download"
        stage.mkdir()
        plain = [dict(row) for row in selected]
        rows_path = stage / rows_filename(format)
        if format == "parquet":
            _write_parquet(plain, rows_path)
        else:
            _write_jsonl(plain, rows_path)
        record = FetchRecord(
            source=adapter.name,
            dataset=provenance.dataset,
            split=split,
            revision=provenance.revision,
            fingerprint=provenance.fingerprint,
            row_count=len(selected),
            format=format,
            rows_file=rows_path.name,
            rows_sha256=hashlib.sha256(rows_path.read_bytes()).hexdigest(),
        )
        (stage / FETCH_FILE).write_text(json_text(record.model_dump(mode="json")), "utf-8")
        if output.exists():
            raise FileExistsError(f"output already exists: {output}")
        stage.rename(output)
    return record


def load_fetch(path: str | Path) -> FetchRecord:
    """Read and verify a download directory's provenance record."""
    root = Path(path)
    if root.is_file():
        root = root.parent
    record = FetchRecord.model_validate_json((root / FETCH_FILE).read_text("utf-8"))
    rows_path = root / record.rows_file
    if not rows_path.is_file():
        raise ValueError(f"missing {record.rows_file} next to {FETCH_FILE}")
    if hashlib.sha256(rows_path.read_bytes()).hexdigest() != record.rows_sha256:
        raise ValueError(f"{record.rows_file} does not match {FETCH_FILE}")
    return record
