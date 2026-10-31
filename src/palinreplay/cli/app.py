"""CLI: prepare tasks/images and record, inspect or export Agent trajectories.

The model client lives in ``palinreplay.llm`` as a library; there is no CLI for it.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

import palinreplay.cli.preload as preload_cli
import palinreplay.cli.record as record_cli
from palinreplay.cli.outputs import conversion_name, default_output
from palinreplay.tasks import convert, download_dataset, json_text, load_task, prepare_images


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="palinreplay")
    commands = parser.add_subparsers(dest="command", required=True)

    download = commands.add_parser("download", help="fetch raw data or images")
    download_commands = download.add_subparsers(dest="download_command", required=True)
    dataset = download_commands.add_parser("dataset", help="download raw dataset rows")
    dataset.add_argument("--source", required=True, help="source adapter, e.g. swebench_lite")
    dataset.add_argument("--output", type=Path, help="new directory (default: outputs/download/DATE_NAME)")
    dataset.add_argument("--split", default="test", help="dataset split to fetch (default: test)")
    dataset.add_argument("--revision", help="Hugging Face revision; default is the resolved HEAD")
    dataset.add_argument("--limit", type=int, help="keep the first N rows")
    dataset.add_argument(
        "--format",
        choices=["jsonl", "parquet"],
        default="jsonl",
        help="rows file format (default: jsonl; parquet needs the parquet extra)",
    )

    mirror = download_commands.add_parser("mirror", help="pull and verify task images")
    mirror.add_argument(
        "dataset_dir", type=Path, metavar="path", help="converted task package directory"
    )
    mirror.add_argument("--archive-dir", type=Path, help="also save images as docker tar")

    convert_cmd = commands.add_parser("convert", help="convert local data into a task package")
    convert_cmd.add_argument("--source", help="source adapter; inferred from fetch.json if omitted")
    convert_cmd.add_argument(
        "--input", type=Path, required=True, help="download directory or local JSON/JSONL/parquet"
    )
    convert_cmd.add_argument(
        "--output", type=Path, help="new directory (default: outputs/convert/DATE_NAME)"
    )
    convert_cmd.add_argument("--instance-id", action="append", dest="ids", help="repeatable")
    convert_cmd.add_argument("--limit", type=int, help="keep the first N selected tasks")

    inspect = commands.add_parser("inspect", help="validate and show one converted task")
    inspect.add_argument("path", type=Path)
    inspect.add_argument("--agent-input", action="store_true", help="hide evaluation materials")

    record_cli.add_commands(commands)
    preload_cli.add_commands(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "record":
        return record_cli.execute(args)
    if args.command == "preload":
        return preload_cli.execute(args)
    try:
        if args.command == "download" and args.download_command == "dataset":
            if args.output is None:
                args.output = default_output("download", args.source)
            record = download_dataset(
                args.output,
                source=args.source,
                split=args.split,
                revision=args.revision,
                limit=args.limit,
                format=args.format,
            )
            print(f"Downloaded {record.row_count} rows -> {args.output} ({record.rows_file})")
            print(f"Source: {record.dataset} split={record.split} revision={record.revision}")
        elif args.command == "download" and args.download_command == "mirror":
            result = prepare_images(
                args.dataset_dir, archive_dir=args.archive_dir, show_progress=True,
            )
            print(
                f"Prepared {len(result.images)} unique images; wrote {args.dataset_dir}/images.json"
            )
        elif args.command == "convert":
            if args.output is None:
                args.output = default_output("convert", conversion_name(args.input, args.source))
            manifest = convert(
                args.output,
                source=args.source,
                input_path=args.input,
                ids=args.ids,
                limit=args.limit,
            )
            print(f"Converted {len(manifest.tasks)} tasks -> {args.output}")
            print("Image references are unresolved; no image was pulled or container started.")
        elif args.command == "inspect":
            task = load_task(args.path)
            document = task.agent_input() if args.agent_input else task.spec.model_dump(mode="json")
            print(json_text(document), end="")
        return 0
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
            for item in exc.errors(include_input=False, include_context=False, include_url=False)
        )
        print(f"error: invalid task data: {details}", file=sys.stderr)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
    return 1
