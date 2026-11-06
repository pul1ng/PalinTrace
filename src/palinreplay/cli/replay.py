"""Minimal fixed-history Replay command."""

import sys
from pathlib import Path

from palinreplay.cli.common import cancellation_signals
from palinreplay.cli.outputs import default_output, record_name
from palinreplay.cli.replay_progress import ReplayProgressDisplay
from palinreplay.storage.store import store_path


def add_commands(commands):
    replay = commands.add_parser("replay", help="re-execute a fixed committed historical range")
    replay.add_argument("source", type=Path)
    replay.add_argument("--from", dest="start", required=True, help="baseline C number or full node ID")
    replay.add_argument("--to", dest="end", help="inclusive end (default: fixed last committed node)")
    replay.add_argument("--run", dest="run_id", help="source Run ID")
    replay.add_argument("--output", type=Path, help="new directory (default: outputs/replay/DATE_TASK)")
    replay.add_argument("--services-config", type=Path,
                        help="JSON file listing services to prepare before historical execution")
    replay.add_argument("--progress", choices=("auto", "plain", "off"), default="auto",
                        help="auto: live progress on a terminal, plain logs when redirected; off: no progress")


def execute(args):
    from palinreplay.replay.session import run_replay

    try:
        source = store_path(args.source)
        output = args.output or default_output("replay", record_name(source))
        with cancellation_signals(), ReplayProgressDisplay(args.progress) as progress:
            report = run_replay(source, args.start, args.end, run_id=args.run_id, output=output,
                                progress=progress, services_config=args.services_config)
        execution = report["execution_status"]
        print(f"Replay {execution.upper()} -> {output}")
        print("  完整一致性校验未执行")
        if report.get("source_start"):
            print(f"  源 C{report['source_start']['index']} (引用)")
        print(f"  Actual nodes: {report.get('actual_node_count', 0)}")
        for mapping in report.get("mappings", []):
            print(f"  Source C{mapping['source_index']} -> C{mapping['index']}")
        if report.get("error"):
            print(f"error: {report['error']}", file=sys.stderr)
        for detail in report.get("cleanup_errors", []):
            print(f"cleanup error: {detail}", file=sys.stderr)
        if report.get("exit_mismatch"):
            first = report["exit_mismatch"]
            print(f"  Exit code mismatch at source C{first['source_index']},"
                  f" tool {first['tool_ordinal']}: {first['source_exit_code']} -> {first['actual_exit_code']}"
                  f" (details: {output / 'report.json'})")
        return {"completed": 0, "stopped": 2, "failed": 1, "cancelled": 130}[execution]
    except KeyboardInterrupt:
        print("Replay cancelled", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
