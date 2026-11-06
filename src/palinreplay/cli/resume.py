"""CLI entry point for continuing a saved trajectory on an independent branch."""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from palinreplay.cli.common import cancellation_signals
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


def execute(args):
    phase = "configuration"
    session = failure = None
    try:
        with cancellation_signals():
            model = _load_model(args.llm_config)
            phase = "source selection"
            start = _select_start(args, model)
            phase = "output selection"
            output = _output_path(args, start)
            print(f"Source: C{start.node.index} ({start.node.node_id}), Run {start.node.run_id}")
            print(f"Output: {output}")
            phase = "restoration"
            from palinreplay.resume import (
                decide_resume_start,
                restore_resume_start,
                run_resume_steps,
            )

            with restore_resume_start(start, output=output,
                                      services_config=args.services_config) as session:
                print(f"Restored: C{session.start.node.index}; R1 passed")
                phase = "first decision"
                first = decide_resume_start(session, instruction=args.instruction,
                                            llm_config=args.llm_config)
                print(f"First decision: {len(first.messages)} input messages")
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
    return code
