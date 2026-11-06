"""CLI entry point for continuing a saved trajectory on an independent branch."""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from palinreplay.cli.common import cancellation_signals
from palinreplay.cli.outputs import default_output
from palinreplay.cli.resume_progress import ResumeProgressDisplay
from palinreplay.core.errors import RecordUsageError
from palinreplay.core.models import Run
from palinreplay.core.progress import ProgressEvent
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
    resume.add_argument("--progress", choices=("auto", "plain", "off"), default="auto",
                        help="auto: live progress on a terminal, plain logs when redirected; off: no progress")


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


# These existing failure paths retain secondary faults only as exception notes.
_FAILURE_NOTES = (
    "Decision evidence write also failed:", "R1 evidence write also failed:",
    "Resume environment cleanup also failed:", "Resume cleanup also failed:",
    "Resume report write also failed:", "Resume recording report also failed:",
    "Resume progress could not be read:", "preparation evidence could not be saved:",
    "attempt termination could not be persisted:", "Recorder close also failed:",
    "resource cleanup also failed:", "cleanup diagnostic could not be persisted:",
)


def _has_failure(report, error):
    from palinreplay.record.calls import RecordingError

    decision, recording = report.get("decision", {}), report.get("recording", {})
    if (report.get("cleanup_errors") or report.get("report_error")
            or recording.get("report_error") or recording.get("progress_error")
            or report.get("preparation", {}).get("evidence_errors")
            or decision.get("status") == "failed" or recording.get("status") == "failed"):
        return True
    evidence = [report.get(key, {}) for key in
                ("restore_error", "services_error", "preparation_error", "delivery_error", "caller")]
    evidence.extend((decision.get("error", {}), recording.get("error", {})))
    for item in evidence:
        if item.get("error_type") not in (None, "KeyboardInterrupt"):
            return True
        if any(note.startswith(_FAILURE_NOTES) for note in item.get("notes", [])):
            return True
    pending, seen = [error] if error is not None else [], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if any(note.startswith(_FAILURE_NOTES) for note in getattr(current, "__notes__", [])):
            return True
        if isinstance(current, RecordingError):
            pending.extend(item for item in (current.recording_error, current.backend_error,
                                            current.diagnostic_error) if item is not None)
        elif isinstance(current, Exception):
            return True
        pending.extend(item for item in (current.__cause__, current.__context__) if item is not None)
    return False


def _cancelled(report, error):
    return (isinstance(error, KeyboardInterrupt)
            or report.get("caller", {}).get("error_type") == "KeyboardInterrupt"
            or any(report.get(part, {}).get("status") == "interrupted"
                   for part in ("decision", "recording")))


def _exit_code(report, error):
    if _has_failure(report, error):
        return 1
    if _cancelled(report, error):
        return 130
    if report.get("recording", {}).get("stop_reason") == "incomplete_tool_group":
        return 3
    return 1 if report.get("status") == "failed" else 0


def _node_label(node):
    return f"C{node['index']} ({node['node_id']})"


def _print_summary(report, start, output, report_path, code, failure, phase):
    if not report:
        return
    recording = report.get("recording")
    decision = report.get("decision", {})
    if recording is not None:
        stop = recording.get("stop_reason", "unknown")
    else:
        stop = "interrupted" if _cancelled(report, failure) else "failed" if code == 1 else "not recorded"
    status = {0: "FINISHED", 1: "FAILED", 3: "INCOMPLETE", 130: "CANCELLED"}[code]
    print(f"Resume {status}")
    source = report.get("node") or {
        "index": start.node.index, "node_id": start.node.node_id, "run_id": start.node.run_id,
    }
    print(f"  Source: {_node_label(source)}, Run {source['run_id']}")
    if recording is None:
        complete = decision.get("complete_nodes", 0)
        requests, tools = decision.get("model_requests", 0), 0
        branch, last = "not created", None
    else:
        complete = None if recording.get("progress_error") else recording.get("complete_nodes")
        requests, tools = recording.get("total_model_requests"), recording.get("tool_executions")
        branch = recording.get("run_id", "unknown" if recording.get("progress_error") else "not created")
        last = recording.get("last_complete_node")
    print(f"  Branch Run: {branch}")
    print(f"  New complete nodes: {complete if complete is not None else 'unknown'}")
    if complete == 0:
        position = f"source {_node_label(source)} (reference)"
    elif complete is not None and last is not None:
        position = _node_label(last)
    else:
        position = "unknown (could not confirm saved progress)"
    print(f"  Last complete node: {position}")
    print(f"  Model requests: {requests if requests is not None else 'unknown'}")
    print(f"  Tool executions (attempts): {tools if tools is not None else 'unknown'}")
    print(f"  Stop reason: {stop}")
    if code == 1:
        stage = (report.get("recording", {}).get("failure_stage")
                 or report.get("restore_error", {}).get("phase")
                 or decision.get("error", {}).get("category") or phase)
        print(f"  Failure phase: {stage}")
    if _cancelled(report, failure):
        print("  Cancellation: requested")
    cleanup = ("completed" if report.get("cleanup_confirmed") else
               "failed" if report.get("cleanup_errors") else "unconfirmed")
    print(f"  Cleanup: {cleanup}")
    for detail in report.get("cleanup_errors", []):
        print(f"  Cleanup error: {detail['resource']} ({detail.get('error_type', 'unconfirmed')})")
    if report.get("report_error") or (recording or {}).get("report_error"):
        print("  Report write: failed; summary uses retained in-memory evidence")
    print(f"  Output: {output}")
    if recording is not None and recording.get("store"):
        print(f"  Branch Store: {recording['store']}")
    if report_path is not None:
        try:
            saved = report_path.is_file()
        except OSError:
            saved = False
        if saved:
            print(f"  Report: {report_path}")
        else:
            print("  Report: unavailable (file not confirmed)")


def execute(args):
    with ResumeProgressDisplay(args.max_steps, args.progress) as progress:
        return _execute(args, progress)


def _execute(args, progress):
    phase = "configuration"
    start = output = session = failure = None
    try:
        with cancellation_signals():
            progress(ProgressEvent("preparing"))
            model = _load_model(args.llm_config)
            phase = "source selection"
            start = _select_start(args, model)
            phase = "output selection"
            output = _output_path(args, start)
            print(f"Source: C{start.node.index} ({start.node.node_id}), Run {start.node.run_id}", flush=True)
            print(f"Output: {output}", flush=True)
            phase = "restoration"
            from palinreplay.resume import (
                decide_resume_start,
                restore_resume_start,
                run_resume_steps,
            )

            with restore_resume_start(start, output=output,
                                      services_config=args.services_config, progress=progress) as session:
                preparation = session.report["preparation"]["status"]
                print(f"Restored: C{session.start.node.index}; R1 passed; services {preparation}", flush=True)
                phase = "first decision"
                first = decide_resume_start(session, instruction=args.instruction,
                                            llm_config=args.llm_config, defer_format_errors=True)
                phase = "recording"
                run_resume_steps(session, first, llm_config=args.llm_config)
                phase = "cleanup"
    except KeyboardInterrupt as error:
        failure = error
        print("error: Resume cancelled", file=sys.stderr)
    except ValidationError as error:
        failure = error
        fields = ", ".join(
            ".".join(map(str, item["loc"]))
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        )
        print(f"error: invalid Resume {phase} fields: {fields}", file=sys.stderr)
    except RecordUsageError as error:
        failure = error
        print(f"error: {error}", file=sys.stderr)
    except FileExistsError as error:
        failure = error
        print("error: output already exists; choose a new output path", file=sys.stderr)
    except FileNotFoundError as error:
        failure = error
        print(f"error: Resume {phase} file or directory not found", file=sys.stderr)
    except Exception as error:
        failure = error
        print(f"error: Resume {phase} failed ({type(error).__name__})", file=sys.stderr)
    report = getattr(failure, "resume_report", None)
    if report is None:
        report = session.report if session is not None else {}
    code = _exit_code(report, failure)
    if code == 1 and _cancelled(report, failure):
        print("error: Resume also has a recording, report or resource failure", file=sys.stderr)
    report_path = getattr(failure, "resume_report_path", None)
    if report_path is None and session is not None:
        report_path = session.report_path
    recording = report.get("recording", {})
    status = recording.get("stop_reason") if code == 0 else {
        1: "failed", 3: "incomplete", 130: "cancelled",
    }[code]
    try:
        progress(ProgressEvent("finished", status=status,
                               completed_steps=recording.get("complete_nodes"),
                               step=(recording.get("last_complete_node") or {}).get("index")))
    except KeyboardInterrupt:
        pass  # A final display update cannot replace the saved outcome.
    _print_summary(report, start, output, report_path, code, failure, phase)
    return code
