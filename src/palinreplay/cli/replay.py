"""Minimal fixed-history Replay command."""

import sys
from pathlib import Path

from palinreplay.cli.common import cancellation_signals
from palinreplay.cli.outputs import default_output, record_name
from palinreplay.cli.record import store_path


def add_commands(commands):
    replay = commands.add_parser("replay", help="re-execute a fixed committed historical range")
    replay.add_argument("source", type=Path)
    replay.add_argument("--from", dest="start", required=True, help="baseline C number or full node ID")
    replay.add_argument("--to", dest="end", help="inclusive end (default: fixed last committed node)")
    replay.add_argument("--run", dest="run_id", help="source Run ID")
    replay.add_argument("--output", type=Path, help="new directory (default: outputs/replay/DATE_TASK)")


def execute(args):
    from palinreplay.replay.session import run_replay

    try:
        source = store_path(args.source)
        output = args.output or default_output("replay", record_name(source))
        with cancellation_signals():
            report = run_replay(source, args.start, args.end, run_id=args.run_id, output=output)
        label = {"match": "MATCH", "difference": "DIFFERENCE", "failed": "FAILED"}[report["status"]]
        print(f"Replay {label} -> {output}")
        for mapping in report.get("mappings", []):
            print(f"  Source C{mapping['source_index']} -> C{mapping['index']}")
        if report.get("error"):
            print(f"error: {report['error']}", file=sys.stderr)
        if report.get("first_difference"):
            print(f"  Stopped: {report['stop_reason']} (details: {output / 'report.json'})")
        return {"match": 0, "difference": 2, "failed": 1}[report["status"]]
    except KeyboardInterrupt:
        print("Replay cancelled", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
