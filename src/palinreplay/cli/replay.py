"""Minimal fixed-history Replay command."""

import sys
from pathlib import Path

from palinreplay.cli.common import cancellation_signals
from palinreplay.cli.outputs import default_output, record_name
from palinreplay.cli.record import store_path
from palinreplay.cli.replay_progress import ReplayProgressDisplay


def add_commands(commands):
    replay = commands.add_parser("replay", help="re-execute a fixed committed historical range")
    replay.add_argument("source", type=Path)
    replay.add_argument("--from", dest="start", required=True, help="baseline C number or full node ID")
    replay.add_argument("--to", dest="end", help="inclusive end (default: fixed last committed node)")
    replay.add_argument("--run", dest="run_id", help="source Run ID")
    replay.add_argument("--output", type=Path, help="new directory (default: outputs/replay/DATE_TASK)")
    replay.add_argument("--services-config", type=Path,
                        help="JSON file listing services to prepare before saving C0")
    replay.add_argument("--validation", dest="validation_mode", choices=("strict", "lenient"),
                        default="lenient",
                        help="stop on differences; lenient permits only file mtime differences (default: lenient)")
    replay.add_argument("--progress", choices=("auto", "plain", "off"), default="auto",
                        help="auto: live progress on a terminal, plain logs when redirected; off: no progress")


def execute(args):
    from palinreplay.replay.session import run_replay

    try:
        source = store_path(args.source)
        output = args.output or default_output("replay", record_name(source))
        with cancellation_signals(), ReplayProgressDisplay(args.progress) as progress:
            report = run_replay(source, args.start, args.end, run_id=args.run_id, output=output,
                                validation_mode=args.validation_mode, progress=progress,
                                services_config=args.services_config)
        execution, validation = report["execution_status"], report["validation_status"]
        print(f"Replay {execution.upper()} -> {output}")
        print(f"  Validation mode: {report['validation_mode']}")
        print(f"  Validation: {validation.upper()}")
        for mapping in report.get("mappings", []):
            print(f"  Source C{mapping['source_index']} -> C{mapping['index']}")
        if report.get("error"):
            print(f"error: {report['error']}", file=sys.stderr)
        if report.get("first_difference"):
            first = report["first_difference"]
            print(f"  First difference: {first['difference_reason']} at source C{first['source_index']}"
                  f" (details: {output / 'report.json'})")
        if execution == "failed":
            return 1
        if validation == "difference":
            return 2
        return 0 if execution == "completed" and validation == "match" else 1
    except KeyboardInterrupt:
        print("Replay cancelled", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
