"""Record commands, kept separate from task preparation and optional Agent imports."""

import json
import sys
from pathlib import Path

from pydantic import ValidationError

from palinreplay.cli.common import cancellation_signals
from palinreplay.cli.outputs import default_output, record_name, task_name
from palinreplay.core.errors import RecordUsageError
from palinreplay.core.models import Run
from palinreplay.record.inspect import export_record, inspect_record, resolve_node
from palinreplay.storage.io import outside_store
from palinreplay.storage.store import RecordStore, store_path
from palinreplay.tasks import json_text


def add_commands(commands):
    record = commands.add_parser("record", help="record, inspect and export Agent trajectories")
    actions = record.add_subparsers(dest="record_command", required=True)
    run = actions.add_parser("run", help="record mini-SWE-agent on one prepared Docker task")
    run.add_argument("dataset_dir", type=Path, help="converted package containing images.json")
    run.add_argument("--task-id", help="required for a multi-task package")
    run.add_argument("--output", type=Path, help="new run directory (default: outputs/record/DATE_TASK)")
    run.add_argument(
        "--llm-config", type=Path, help="private TOML; uses existing defaults if omitted"
    )
    run.add_argument("--max-steps", type=int, default=20)
    run.add_argument("--max-tokens", type=int, default=8192)
    run.add_argument("--time-limit", type=int, default=0, help="native wall seconds, 0 = disabled")
    run.add_argument(
        "--cost-limit", type=float, default=0, help="native estimated cost, 0 = disabled"
    )
    run.add_argument(
        "--retry-attempts", type=int, default=1, help="native attempts per query; SDK retries off"
    )
    run.add_argument("--network", choices=("bridge", "none"), default="bridge")
    run.add_argument("--preload", required=True, help="existing Btrfs C0 base directory")
    run.add_argument(
        "--debug", action="store_true",
        help="show numbered live substeps and a timing summary; timings are always saved",
    )
    run.add_argument(
        "--progress", choices=("auto", "plain", "off"), default="auto",
        help="auto: live progress on a terminal, plain logs when redirected; off: no progress",
    )
    run.add_argument(
        "--public-env",
        action="append",
        default=[],
        metavar="KEY",
        help="additional nonsecret container environment key to capture (repeatable)",
    )

    inspect = actions.add_parser(
        "inspect", help="read metadata; --check also verifies Btrfs snapshot identities"
    )
    inspect.add_argument("path", type=Path, help="run directory or Record store")
    inspect.add_argument("--run", dest="run_id", help="select a run ID")
    selection = inspect.add_mutually_exclusive_group()
    selection.add_argument("--node", help="C0/C1/... or full node ID; includes context and calls")
    selection.add_argument("--execution", help="execution ID; includes raw and accepted contents")
    inspect.add_argument("--check", action="store_true", help="hash all required artifacts")
    inspect.add_argument("--json", action="store_true", help="machine-readable view")

    export = actions.add_parser(
        "export", help="export readable trajectory evidence"
    )
    export.add_argument("path", type=Path)
    export.add_argument(
        "--output", type=Path, help="new export directory (default: outputs/export/DATE_TASK)"
    )
    export.add_argument(
        "--format",
        choices=("trajectory", "bundle"),
        default="trajectory",
        help="Btrfs supports trajectory only; external pools cannot be bundled",
    )

    restore = actions.add_parser(
        "verify-restore", help="restore a node, verify files, then remove test resources"
    )
    restore.add_argument("path", type=Path)
    restore.add_argument("--node", required=True, help="C0/C1/... or full node ID")
    restore.add_argument("--run", dest="run_id")
    restore.add_argument(
        "--report", "--output", dest="report", type=Path,
        help="new JSON report (default: outputs/verify-restore/DATE_TASK_NODE.json)",
    )

    visual = actions.add_parser("visualize", help="generate an offline Btrfs trajectory viewer")
    visual.add_argument("path", type=Path, metavar="RUN_DIR", help="run with report.json and record/")
    visual.add_argument(
        "--output", type=Path, help="new HTML file (default: outputs/visual/DATE_TASK.html)"
    )


def _summary(view):
    verification = view["verification"]
    label = (
        "passed" if verification["ok"] else "failed" if verification["checked"] else "not checked"
    )
    print(f"Integrity: {label}")
    for issue in verification.get("issues", []):
        print(f"  {issue['record']}: {issue['message']}")
    for run in view["runs"]:
        replay = run.get("replay")
        status_label = "record_status" if replay is not None else "status"
        print(f"Run {run['run']['run_id']}  task={run['run']['task_id']}  {status_label}={run['status']}")
        boundary = "Record boundary complete" if replay is not None else "Record complete"
        print(f"  {boundary}: {run['record_complete']}; task success: not evaluated")
        if replay is not None:
            print(f"  Replay execution: {replay['execution_status']}; completed steps: "
                  f"{replay['completed_steps']}/{replay['selected_steps']}")
            usage = replay["history_usage"]
            print(f"  Historical answer supplies: {usage['historical_answer_supplies']}; "
                  f"dispatched tool calls: {usage['dispatched_tool_calls']}; "
                  f"unknown results: {len(usage['unknown_result_execution_ids'])}")
            print(f"  Replay finalization: {replay['finalization']['status']}")
        if run.get("source_start"):
            source = run["source_start"]
            print(f"  Source C{source['index']} (reference)  node={source['node_id']}"
                  f"  store={source['source_store']}")
        for node in run["nodes"]:
            print(f"  {node['label']}  node={node['node_id']}  state={node['state_id']}")
        for call in run["calls"]:
            operation, execution = call["operation"], call["execution"]
            print(
                f"  {operation['kind']} {operation['name']} {execution['execution_id']} {call['phase']}"
            )
        print(
            f"  Incomplete steps: {len(run['incomplete_steps'])}; unknown outcomes: {len(run['unknown_executions'])}"
        )
        for failure in run["failures"]:
            details = failure["detail_content"]["value"]
            brief = {
                key: details[key]
                for key in ("phase", "error", "error_type", "reason")
                if key in details
            }
            print(f"  Failure {failure['category']}: {json.dumps(brief, ensure_ascii=True)}")
    if "storage" in view:
        print(
            f"Store: {view['storage']['store_file_bytes']} bytes; {view['storage']['state_count']} states"
        )


def _flow_timing_summary(flow):
    print("Record 流程计时 (秒; 按文档编号, 同层耗时可相加, S1-S6 包含在捕获中):")
    print("  子项已计入主编号; 多段累计只相加同一层级。")

    def print_row(row, indent):
        value = "未执行/未埋点" if row["duration_sec"] is None else f"{row['duration_sec']:.6f}s"
        raised = f", 抛出控制流/错误 {row['raised']} 次" if row["raised"] else ""
        count = f", 累计 {row['count']} 段" if row["count"] > 1 else ""
        print(f"{indent}{row['id']} {row['label']}: {value}{count}{raised}")

    def print_parts(rows, indent):
        for row in rows:
            for part in row.get("parts", []):
                if part["duration_sec"] is not None:
                    print_row(part, indent)

    for phase in flow["phases"]:
        print(f"  {phase['label']}: {phase['duration_sec']:.6f}s")
        for row in phase["substeps"]:
            print_row(row, "    ")
            print_parts([row], "      ")
        print(f"    其他调度与未归类开销: {phase['other_sec']:.6f}s")

    def cells(rows):
        return "  ".join(
            f"{r['id']}=" + ("--" if r["duration_sec"] is None else f"{r['duration_sec']:.6f}")
            for r in rows
        )

    print("  逐步明细 (-- 表示未执行/未埋点; 有捕获计时不代表提交成功):")
    for group in flow["steps"]:
        if group["substeps"]:
            print(f"    Step {group['index']}: {cells(group['substeps'])}")
            print_parts(group["substeps"], "      ")
        capture = group["capture"]
        if capture["duration_sec"] is not None:
            status = "raised" if capture["raised"] else "ok"
            print(f"    C{group['index']} 捕获主体: {capture['duration_sec']:.6f}s ({status})")
            print(f"      {cells(capture['substeps'])}")
            print_parts(capture["substeps"], "        ")
            print(f"      其他边界开销={capture['other_sec']:.6f}s")
    if not flow["step_details_complete"]:
        print("  逐步明细达到上限, 阶段汇总仍完整。")


def _timing_summary(timing):
    if timing.get("flow"):
        _flow_timing_summary(timing["flow"])
    print("Debug timings (seconds; inclusive includes children; self excludes them):")
    print(f"  {'stage':32} {'calls':>6} {'inclusive':>11} {'self':>11}")
    for name, values in sorted(
        timing["totals"].items(), key=lambda item: item[1]["self_sec"], reverse=True
    ):
        print(
            f"  {name:32} {values['count']:6d} "
            f"{values['duration_sec']:11.3f} {values['self_sec']:11.3f}"
        )
    for row in timing["spans"]:
        if row["name"] == "node.capture_commit":
            print(f"  C{row['step']}: capture/commit {row['duration_sec']:.3f}s ({row['status']})")
    if not timing["timeline_complete"]:
        print(f"  Timeline capped: {timing['dropped_spans']} spans omitted; totals remain complete")


def execute(args) -> int:
    try:
        if args.record_command == "run":
            if args.output is None:
                args.output = default_output("record", task_name(args.dataset_dir, args.task_id))
            from palinreplay.cli.progress import RecordProgressDisplay
            from palinreplay.record.run import RunOptions, run_record

            options = RunOptions(
                max_steps=args.max_steps,
                max_tokens=args.max_tokens,
                time_limit=args.time_limit,
                cost_limit=args.cost_limit,
                retry_attempts=args.retry_attempts,
                network=args.network,
                public_env=tuple(args.public_env),
                preload=args.preload,
            )
            with cancellation_signals(), RecordProgressDisplay(
                options.max_steps, args.progress
            ) as progress:
                report = run_record(
                    args.dataset_dir,
                    args.output,
                    task_id=args.task_id,
                    llm_config=args.llm_config,
                    options=options,
                    progress=progress,
                    debug=args.debug,
                )
            print(f"Record: {report['status']} -> {args.output / 'record'}")
            print(f"Report: {args.output / 'report.json'}")
            if args.debug and "debug_timing" in report:
                _timing_summary(report["debug_timing"])
            return report["exit_code"]
        source = store_path(args.path)
        if args.record_command == "inspect":
            view = inspect_record(
                source,
                run_id=args.run_id,
                node=args.node,
                execution=args.execution,
                check=args.check,
            )
            if args.json or args.node or args.execution:
                print(json_text(view), end="")
            else:
                _summary(view)
            return 1 if view["verification"]["ok"] is False else 0
        if args.record_command == "export":
            if args.output is None:
                args.output = default_output("export", record_name(source))
            result = export_record(source, args.output, bundle=args.format == "bundle")
            print(f"Exported {result['kind']} -> {args.output}")
            return 0
        if args.record_command == "visualize":
            from palinreplay.record.visualize import render_record

            if args.output is None:
                args.output = default_output("visual", record_name(source), suffix=".html")
            data = render_record(args.path, args.output)
            print(f"Visualization: {args.output}")
            print(f"Nodes: {len(data['nodes'])}; events: {len(data['events'])}")
            return 0
        if args.record_command == "verify-restore":
            from palinreplay.record.restore import restore_node
            from palinreplay.sandbox.client import connect_docker, docker_endpoint

            with RecordStore.open(source) as store:
                node = resolve_node(store, args.node, args.run_id)
                default_report = args.report is None
                if default_report:
                    name = store.get(Run, node.run_id).task_id + "_" + args.node
                    args.report = default_output("verify-restore", name, suffix=".json")
                outside_store(source, args.report)
                if args.report.exists() or args.report.is_symlink():
                    raise FileExistsError("restore report already exists")
                if default_report:
                    args.report.parent.mkdir(parents=True, exist_ok=True)
                endpoint = docker_endpoint()
                client = connect_docker(600)
                try:
                    with restore_node(
                        store,
                        node.node_id,
                        client=client,
                        docker_host=endpoint,
                        report_path=args.report,
                    ):
                        pass
                finally:
                    client.close()
            print(f"File restoration verified; test resources removed. Report: {args.report}")
            return 0
    except KeyboardInterrupt:
        print("error: cancelled", file=sys.stderr)
        return 130
    except ValidationError as error:
        fields = ", ".join(
            ".".join(map(str, item["loc"]))
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        )
        print(f"error: invalid configuration or record fields: {fields}", file=sys.stderr)
    except RecordUsageError as error:
        print(f"error: {error}", file=sys.stderr)
    except FileExistsError:
        print("error: output already exists; choose a new output path", file=sys.stderr)
    except ImportError:
        print(
            "error: install Record dependencies with uv sync --extra mini --extra docker",
            file=sys.stderr,
        )
    except Exception as error:
        # Do not print arbitrary SDK/Docker exception strings or credential paths.
        # Durable run/restore reports and verified evidence carry diagnostics.
        print(
            f"error: record {args.record_command} failed ({type(error).__name__})", file=sys.stderr
        )
    return 1
