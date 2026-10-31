"""Shared SWE-bench row mapping; variant modules only declare their differences."""

import json
from collections.abc import Sequence
from typing import Literal

from palinreplay.tasks.model import (
    DatasetSource,
    EnvironmentSpec,
    ImageSpec,
    RepositorySpec,
    validate_task_id,
)
from palinreplay.tasks.sources.base import BuildOptions, Draft, Row, SourceSpec, register

ARCH_PLATFORMS = {"x86_64": "linux/amd64", "arm64": "linux/arm64"}

# Public dataset IDs. A variant accepts only its own ID (or its short keyword).
DATASETS = {
    "full": "princeton-nlp/SWE-bench",
    "lite": "princeton-nlp/SWE-bench_Lite",
    "verified": "princeton-nlp/SWE-bench_Verified",
}

# Execution defaults from the researched mini-SWE-agent SWE-bench config.
# They belong to this benchmark family, not to the generic task format.
ENVIRONMENT = {
    "workdir": "/testbed",
    "shell": ["bash", "-c"],
    "env": {
        "PAGER": "cat",
        "MANPAGER": "cat",
        "LESS": "-R",
        "PIP_PROGRESS_BAR": "off",
        "TQDM_DISABLE": "1",
        "BASH_ENV": "/root/.bashrc",
    },
    "timeout_sec": 60,
}


def text(row: Row, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"SWE-bench record requires non-blank string field: {key}")
    return value


def row_id(row: Row) -> str:
    return validate_task_id(text(row, "instance_id"))


def resolve_image(row: Row, options: BuildOptions) -> ImageSpec:
    instance_id = row_id(row)
    if options.image_map is not None and instance_id in options.image_map:
        reference, source = options.image_map[instance_id], "mapping"
    elif row.get("image_name"):
        reference, source = row["image_name"], "image_name"
    elif row.get("docker_image"):
        reference, source = row["docker_image"], "docker_image"
    else:
        # Public SWE-bench naming convention; not a registry availability check.
        suffix = instance_id.lower().replace("__", "_1776_")
        reference = f"docker.io/swebench/sweb.eval.{options.arch}.{suffix}:latest"
        source = "convention"
    return ImageSpec(reference=reference, platform=ARCH_PLATFORMS[options.arch], source=source)


def build(row: Row, options: BuildOptions, *, variant: str) -> Draft:
    version = row.get("version")
    return Draft(
        instruction=text(row, "problem_statement"),
        environment=EnvironmentSpec(image=resolve_image(row, options), **ENVIRONMENT),
        repository=RepositorySpec(
            name=text(row, "repo"), base_commit=text(row, "base_commit"), version=version
        ),
        evaluation_kind=f"swebench-{variant}-source",
        evaluation_files={
            # Field-level canonical JSON keeps every column for the verifier side.
            "source.json": json.dumps(dict(row), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        },
    )


def fetch(
    dataset: str, split: str, revision: str | None, *, variant: str
) -> tuple[Sequence[Row], DatasetSource]:
    """Explicit online fetch; refuses a dataset that does not belong to this variant."""
    expected = DATASETS[variant]
    requested = DATASETS.get(dataset, dataset)
    if requested != expected:
        raise ValueError(
            f"source swebench_{variant} targets {expected!r}, not {dataset!r}; "
            "choose the matching --source for download dataset instead"
        )
    try:
        from datasets import load_dataset as hf_load_dataset
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise RuntimeError(
            'Hugging Face import requires: pip install "palinreplay[swebench]"'
        ) from exc
    resolved = HfApi().dataset_info(requested, revision=revision).sha
    if not resolved:
        raise ValueError("could not resolve Hugging Face dataset revision")
    data = hf_load_dataset(requested, split=split, revision=resolved)
    source = DatasetSource(
        source=f"swebench_{variant}",
        dataset=requested,
        split=split,
        revision=resolved,
        fingerprint=getattr(data, "_fingerprint", None),
    )
    return [dict(row) for row in data], source


def variant_spec(
    variant: Literal["full", "lite", "verified"], *, aliases: tuple[str, ...] = ()
) -> SourceSpec:
    """Register one variant; a variant file adds no other logic."""

    def build_row(row: Row, options: BuildOptions) -> Draft:
        return build(row, options, variant=variant)

    def fetch_rows(dataset: str, split: str, revision: str | None):
        return fetch(dataset, split, revision, variant=variant)

    return register(
        SourceSpec(
            name=f"swebench_{variant}",
            aliases=aliases,
            row_id=row_id,
            build=build_row,
            fetch=fetch_rows,
            extra="swebench",
            dataset=DATASETS[variant],
        )
    )
