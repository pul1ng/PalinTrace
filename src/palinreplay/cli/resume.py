"""CLI entry point for continuing a saved trajectory on an independent branch."""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from palinreplay.cli.outputs import default_output
from palinreplay.core.errors import RecordUsageError
from palinreplay.core.models import Run
from palinreplay.storage.store import RecordStore


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


def _output_path(args, start):
    if args.output is not None:
        return args.output
    with RecordStore.open(start.source_store, read_only=True) as store:
        task = store.get(Run, start.node.run_id).task_id
    return default_output("resume", task)


def execute(args):
    phase = "configuration"
    try:
        model = _load_model(args.llm_config)
        phase = "source selection"
        start = _select_start(args, model)
        phase = "output selection"
        output = _output_path(args, start)
        print(f"Source: C{start.node.index} ({start.node.node_id}), Run {start.node.run_id}")
        print(f"Output: {output}")
        phase = "restoration"
        from palinreplay.resume import decide_resume_start, restore_resume_start, run_resume_steps

        with restore_resume_start(start, output=output, services_config=args.services_config) as session:
            print(f"Restored: C{session.start.node.index}; R1 passed")
            phase = "first decision"
            first = decide_resume_start(session, instruction=args.instruction, llm_config=args.llm_config)
            print(f"First decision: {len(first.messages)} input messages")
            phase = "recording"
            run_resume_steps(session, first, llm_config=args.llm_config)
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
