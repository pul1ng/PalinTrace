"""CLI entry point for continuing a saved trajectory on an independent branch."""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from palinreplay.core.errors import RecordUsageError


def _positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _instruction(value):
    if not value.strip():
        raise argparse.ArgumentTypeError("instruction must not be blank")
    return value


def add_commands(commands):
    resume = commands.add_parser("resume", help="fork a saved node and make new model decisions")
    resume.add_argument("source", type=Path, metavar="SOURCE",
                        help="source recording directory or its record/ Store")
    resume.add_argument("--from", dest="start", required=True, metavar="NODE",
                        help="source C number or full node ID")
    resume.add_argument("--run", dest="run_id", metavar="RUN_ID", help="source Run ID")
    resume.add_argument("--max-steps", type=_positive_int, default=20, metavar="N",
                        help="new decision budget, including the first request (default: 20); "
                             "the model may submit early")
    resume.add_argument("--llm-config", type=Path, metavar="FILE",
                        help="model configuration TOML (default: existing config loading rules)")
    resume.add_argument("--instruction", type=_instruction, metavar="TEXT",
                        help="append one nonblank user message (default: no extra message)")
    resume.add_argument("--services-config", type=Path, metavar="FILE",
                        help="service manifest (default: skip service preparation)")
    resume.add_argument("--output", type=Path, metavar="DIR",
                        help="new independent directory (default: outputs/resume/YYMMDD_HHMMSS_TASK/)")


def _load_model(config_path):
    from palinreplay.llm import LLMConfig

    return LLMConfig.load(config_path).model


def _select_start(args, model):
    from palinreplay.resume import select_resume_start

    return select_resume_start(args.source, args.start, run_id=args.run_id,
                               model=model, max_steps=args.max_steps)


def execute(args):
    phase = "configuration"
    try:
        model = _load_model(args.llm_config)
        phase = "source selection"
        start = _select_start(args, model)
        print(f"Source: C{start.node.index} ({start.node.node_id}), Run {start.node.run_id}")
        return 0
    except ValidationError as error:
        fields = ", ".join(
            ".".join(map(str, item["loc"]))
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        )
        print(f"error: invalid model configuration fields: {fields}", file=sys.stderr)
    except RecordUsageError as error:
        print(f"error: {error}", file=sys.stderr)
    except FileNotFoundError:
        print(f"error: Resume {phase} file or directory not found", file=sys.stderr)
    except Exception as error:
        print(f"error: Resume {phase} failed ({type(error).__name__})", file=sys.stderr)
    return 1
