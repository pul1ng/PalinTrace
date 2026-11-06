"""Generate a reusable service manifest from one complete Record run."""

import sys
from pathlib import Path

from palinreplay.cli.common import cancellation_signals
from palinreplay.cli.outputs import default_output, record_name
from palinreplay.storage.store import store_path


def add_commands(commands):
    preprocess = commands.add_parser(
        "preprocess", help="analyze a complete Record and write services.json"
    )
    preprocess.add_argument("source", type=Path, help="Record store or run directory containing record/")
    preprocess.add_argument("--run", dest="run_id", help="select one Run ID")
    preprocess.add_argument(
        "--llm-config", type=Path,
        help="dedicated preprocessing TOML (default: config/llm/preprocess_llm.toml)",
    )
    preprocess.add_argument("--output", type=Path, help="new directory (default: outputs/preprocess/DATE_TASK)")


def execute(args) -> int:
    from palinreplay.preprocess.config import load_preprocess_config
    from palinreplay.preprocess.pipeline import run_preprocess

    try:
        config, config_path = load_preprocess_config(args.llm_config)
        source = store_path(args.source)
        output = args.output or default_output("preprocess", record_name(source))
        with cancellation_signals():
            result = run_preprocess(
                source, run_id=args.run_id, config=config, output=output
            )
        manifest = result.manifest
        print(f"Preprocess: {len(manifest.services)} service intervals -> {result.output}")
        print(f"  Manifest: {result.output / 'services.json'}")
        print(f"  Report: {result.output / 'report.json'}")
        print(f"  Config: {config_path}; model={config.llm.model}")
        return 0
    except KeyboardInterrupt:
        print("Preprocess cancelled", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
