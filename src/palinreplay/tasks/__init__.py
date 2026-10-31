"""Project-owned task packages, dataset conversion and image preparation."""

from palinreplay.tasks.download import FetchRecord, download_dataset, load_fetch
from palinreplay.tasks.images import PreparedImage, PreparedImages, prepare_images
from palinreplay.tasks.model import (
    DatasetManifest,
    DatasetSource,
    TaskPackage,
    TaskSpec,
    json_text,
    load_dataset,
    load_task,
)
from palinreplay.tasks.pipeline import convert, convert_rows, input_digest, read_rows
from palinreplay.tasks.sources import (
    Arch,
    BuildOptions,
    Draft,
    Row,
    SourceSpec,
    available,
    register,
    resolve,
)

__all__ = [
    "Arch",
    "BuildOptions",
    "DatasetManifest",
    "DatasetSource",
    "Draft",
    "FetchRecord",
    "PreparedImage",
    "PreparedImages",
    "Row",
    "SourceSpec",
    "TaskPackage",
    "TaskSpec",
    "available",
    "convert",
    "convert_rows",
    "download_dataset",
    "input_digest",
    "json_text",
    "load_dataset",
    "load_fetch",
    "load_task",
    "prepare_images",
    "read_rows",
    "register",
    "resolve",
]
