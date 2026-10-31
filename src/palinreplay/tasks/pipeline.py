"""Generic conversion pipeline; dataset-specific knowledge stays in sources/."""

import hashlib
import json
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from palinreplay.tasks.download import FETCH_FILE, load_fetch
from palinreplay.tasks.model import (
    Artifact,
    DatasetManifest,
    DatasetSource,
    EvaluationSpec,
    TaskEntry,
    TaskSpec,
    file_sha256,
    json_text,
)
from palinreplay.tasks.sources.base import Arch, BuildOptions, Row, SourceSpec, resolve

SUPPORTED_SUFFIXES = {".json", ".jsonl", ".ndjson", ".parquet"}


def _read_parquet(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError(
            'reading .parquet input requires: pip install "palinreplay[parquet]"'
        ) from exc
    try:
        rows = pq.read_table(path).to_pylist()
    except Exception as exc:  # pyarrow raises its own error hierarchy
        raise ValueError(f"could not read parquet file {path.name}: {exc}") from exc
    return rows


def read_rows(path: str | Path, *, label: str) -> list[dict[str, Any]]:
    """JSON array, one object, JSONL/NDJSON lines, or parquet.

    A directory is read in sorted filename order, which matches a Hugging Face
    snapshot ``data/`` directory or a multi-shard dataset download.
    """
    path = Path(path)
    if path.is_dir():
        # A download directory carries provenance; read only its verified rows file.
        if (path / FETCH_FILE).exists():
            record = load_fetch(path)
            rows = read_rows(path / record.rows_file, label=label)
            if len(rows) != record.row_count:
                raise ValueError(f"row count does not match {FETCH_FILE}")
            return rows
        files = sorted(
            item
            for item in path.iterdir()
            if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES
        )
        if not files:
            raise ValueError(
                f"{path} contains no readable data files ({', '.join(sorted(SUPPORTED_SUFFIXES))})"
            )
        rows = [row for item in files for row in read_rows(item, label=label)]
    elif path.suffix.lower() == ".parquet":
        rows = _read_parquet(path)
    else:
        text = path.read_text(encoding="utf-8-sig")
        if path.suffix.lower() in {".jsonl", ".ndjson"}:
            rows = []
            for number, line in enumerate(text.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON at line {number} in {path.name}") from exc
        else:
            data = json.loads(text)
            rows = [data] if isinstance(data, dict) else data
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"{label} input must be rows of objects, not {type(rows).__name__}")
    return rows


def select_rows(
    rows: Iterable[Row],
    *,
    adapter: SourceSpec,
    ids: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[Row]:
    """Validate all row IDs before selection; convert_rows validates the full rows."""
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0):
        raise ValueError("limit must be positive")
    by_id: dict[str, Row] = {}
    for row in rows:
        task_id = adapter.row_id(row)
        if task_id in by_id:
            raise ValueError(f"duplicate task ID: {task_id}")
        by_id[task_id] = row
    if ids is None:
        selected = list(by_id.values())
    else:
        if len(ids) != len(set(ids)):
            raise ValueError("task selection contains duplicates")
        missing = [task_id for task_id in ids if task_id not in by_id]
        if missing:
            raise ValueError(f"unknown task IDs: {', '.join(missing)}")
        selected = [by_id[task_id] for task_id in ids]
    selected = selected[:limit] if limit is not None else selected
    if not selected:
        raise ValueError("selection contains no tasks")
    return selected


def convert_rows(
    rows: Iterable[Row],
    output_dir: str | Path,
    *,
    adapter: SourceSpec,
    source: Any,
    ids: Sequence[str] | None = None,
    limit: int | None = None,
    options: BuildOptions | None = None,
) -> DatasetManifest:
    """Stage every task, then publish once; existing output is never overwritten."""
    options = options or BuildOptions()
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    rows = list(rows)
    selected = select_rows(rows, adapter=adapter, ids=ids, limit=limit)
    # Validate full rows before writing, including those excluded by IDs or limit.
    drafts = {adapter.row_id(row): adapter.build(row, options) for row in rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}-", dir=output.parent) as temporary:
        stage = Path(temporary) / "dataset"
        stage.mkdir()
        entries = []
        for row in selected:
            task_id = adapter.row_id(row)
            draft = drafts[task_id]
            root = stage / task_id
            root.mkdir()
            (root / "instruction.md").write_text(draft.instruction, encoding="utf-8", newline="")
            evaluation = []
            for relative, text in draft.evaluation_files.items():
                target = root / "evaluation" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                evaluation.append(
                    Artifact(path=f"evaluation/{relative}", sha256=file_sha256(target))
                )
            spec = TaskSpec(
                task_id=task_id,
                source=source,
                instruction=Artifact(
                    path="instruction.md", sha256=file_sha256(root / "instruction.md")
                ),
                environment=draft.environment,
                repository=draft.repository,
                evaluation=EvaluationSpec(kind=draft.evaluation_kind, artifacts=evaluation),
            )
            task_path = root / "task.json"
            task_path.write_text(json_text(spec.model_dump(mode="json")), encoding="utf-8")
            entries.append(
                TaskEntry(
                    task_id=task_id,
                    manifest=Artifact(path=f"{task_id}/task.json", sha256=file_sha256(task_path)),
                )
            )
        manifest = DatasetManifest(source=source, tasks=entries)
        (stage / "manifest.json").write_text(json_text(manifest.model_dump(mode="json")), "utf-8")
        if output.exists():
            raise FileExistsError(f"output already exists: {output}")
        stage.rename(output)
    return manifest


def input_digest(path: str | Path) -> str:
    """One digest for a file, or for a directory's sorted data files and names."""
    path = Path(path)
    if not path.is_dir():
        return file_sha256(path)
    digest = hashlib.sha256()
    for item in sorted(
        p for p in path.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    ):
        digest.update(item.name.encode("utf-8"))
        digest.update(bytes.fromhex(file_sha256(item)))
    return digest.hexdigest()


def convert(
    output_dir: str | Path,
    *,
    input_path: str | Path,
    source: str | None = None,
    split: str | None = None,
    revision: str | None = None,
    ids: Sequence[str] | None = None,
    limit: int | None = None,
    arch: Arch = "x86_64",
    image_map: Mapping[str, str] | None = None,
) -> DatasetManifest:
    """Convert local data only; never fetch a dataset or touch Docker.

    A download directory's fetch.json is authoritative. An explicit source must
    resolve to the same adapter. Library-only split/revision labels may annotate
    bare inputs but must not override downloaded provenance.
    """
    if Path(output_dir).exists():
        raise FileExistsError(f"output already exists: {output_dir}")
    path = Path(input_path)
    if path.is_dir() and (path / FETCH_FILE).exists():
        record = load_fetch(path)
        adapter = resolve(record.source)
        if source is not None and resolve(source).name != adapter.name:
            raise ValueError(f"source {source!r} conflicts with {FETCH_FILE}: {record.source!r}")
        for name, value in (("split", split), ("revision", revision)):
            if value is not None and value != getattr(record, name):
                raise ValueError(f"{name} conflicts with {FETCH_FILE}")
        rows = read_rows(path / record.rows_file, label=adapter.name)
        if len(rows) != record.row_count:
            raise ValueError(f"row count does not match {FETCH_FILE}")
        provenance = record.as_dataset_source()
    else:
        if source is None:
            raise ValueError(
                "provide --source for input without fetch.json (use a download directory)"
            )
        adapter = resolve(source)
        rows = read_rows(path, label=adapter.name)
        provenance = DatasetSource(
            source=adapter.name,
            dataset=f"local/{adapter.name}",
            split=split if split is not None else "test",
            revision=revision,
            input_sha256=input_digest(path),
        )
    return convert_rows(
        rows,
        output_dir,
        adapter=adapter,
        source=provenance,
        ids=ids,
        limit=limit,
        options=BuildOptions(arch=arch, image_map=image_map),
    )
