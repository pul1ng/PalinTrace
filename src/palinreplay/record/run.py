"""Own one CLI recording's resources; keep diagnostics outside its immutable facts."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from palinreplay.agents import AgentTask
from palinreplay.agents.mini import MiniSweAgent, load_native
from palinreplay.core.errors import RecordUsageError
from palinreplay.core.models import Failure
from palinreplay.core.progress import ProgressCallback, ProgressEvent, ProgressReporter
from palinreplay.diagnostics.metrics import boundary_summary
from palinreplay.diagnostics.record_timing import flow_scope, record_phase, record_timing
from palinreplay.diagnostics.timing import debug_timing, span
from palinreplay.llm import LLMConfig, mini_model_name
from palinreplay.preload.base import BtrfsPreloadRecord
from palinreplay.record.inspect import inspect_record, storage_metrics
from palinreplay.record.state import BtrfsStateBackend
from palinreplay.sandbox import load_sandbox_task
from palinreplay.sandbox.btrfs import BtrfsSandbox
from palinreplay.sandbox.client import connect_docker
from palinreplay.sandbox.runtime import PUBLIC_ENV_KEYS
from palinreplay.storage.io import write_json
from palinreplay.storage.store import RecordStore


@dataclass(frozen=True)
class RunOptions:
    """Recording settings for a task using an existing Btrfs C0 base."""

    max_steps: int = 20
    max_tokens: int = 8192
    time_limit: int = 0
    cost_limit: float = 0
    retry_attempts: int = 1
    network: str = "bridge"
    public_env: tuple[str, ...] = ()
    preload: str | None = None

    def validate(self):
        import math

        for name in ("max_steps", "max_tokens", "retry_attempts"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise RecordUsageError(f"{name} must be a positive integer")
        if type(self.time_limit) is not int or self.time_limit < 0:
            raise RecordUsageError("time_limit must be a nonnegative integer")
        for name in ("cost_limit",):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise RecordUsageError(f"{name} must be finite and nonnegative")
        if self.network not in ("bridge", "none"):
            raise RecordUsageError("network must be bridge or none")
        if self.preload is None:
            raise RecordUsageError("Btrfs recording requires --preload")
        if any(not key or "=" in key or "\x00" in key for key in self.public_env):
            raise RecordUsageError("public_env must contain valid environment key names")


def run_record(
    dataset_dir: str | Path,
    output: str | Path,
    *,
    task_id: str | None = None,
    llm_config: str | Path | None = None,
    options: RunOptions = RunOptions(),
    completion=None,
    progress: ProgressCallback | None = None,
    debug: bool = False,
) -> dict:
    """Run and always persist a bounded timing tree in the public report.

    ``debug`` only enables detailed timing progress events for display.
    Preflight errors still raise before creating output/resources. Timing never
    stores prompts, commands, credentials or exception messages. The final
    serialization of the timing report itself is outside ``record.total``.
    """
    notify = ProgressReporter(progress)
    with debug_timing(True) as timing:
        with span("record.total"), record_timing(True, progress=notify if debug else None) as flow:
            report = _run_record(
                dataset_dir, output, task_id=task_id, llm_config=llm_config,
                options=options, completion=completion, progress=notify,
            )
        report["debug_timing"] = timing.snapshot()
        report["debug_timing"]["flow"] = flow.snapshot()
        write_json(Path(output) / "report.json", report, replace=True)
        notify(ProgressEvent(
            "finished", status=report["status"], run_end_status=report.get("run_end_status")
        ))
        return report


def _run_record(
    dataset_dir, output, *, task_id, llm_config, options, completion, progress,
) -> dict:
    """Run mini on a prepared task. No pull, no overwrite, no automatic resumption.

    ``completion`` is a library-only deterministic SDK fixture for acceptance;
    the CLI always loads its private LLM config and uses the real client.
    Returns a durable public report, including separate primary/cleanup errors.
    ``progress`` observes public stages/counters; observer errors are nonfatal.
    """
    notify = ProgressReporter(progress)
    notify(ProgressEvent("preparing"))
    with flow_scope("1.1"), span("prepare.validate"):
        options.validate()
        output = Path(output).absolute()
        if output.exists() or output.is_symlink():
            raise FileExistsError(f"output already exists: {output}")
    with flow_scope("1.2", part="1.2.1"), span("prepare.task"):
        binding = load_sandbox_task(dataset_dir, task_id)
        preload = BtrfsPreloadRecord.select(options.preload, binding)
        if output.resolve().is_relative_to(preload.base.path):
            raise RecordUsageError("record output must be outside the frozen preload")
    with flow_scope("1.2", part="1.2.2"), span("prepare.agent_import"):
        load_native()  # Fail incompatible pins before resource creation.
    with flow_scope("1.2", part="1.2.3"), span("prepare.llm_config"):
        credentials = (
            None if completion else LLMConfig.load(llm_config).model_copy(update={"max_retries": 0})
        )
    model = mini_model_name(credentials.model if credentials else "openai/record-fixture")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    started = time.monotonic()
    report = {
        "schema_version": "palinreplay.record-run.v1",
        "status": "in_progress",
        "phase": "preparing",
        "exit_code": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "task_id": binding.task.spec.task_id,
        "dataset_manifest_sha256": binding.dataset_manifest_sha256,
        "images_manifest_sha256": binding.images_manifest_sha256,
        "source": binding.task.spec.source.model_dump(mode="json"),
        "model": model,
        "model_transport": "fixture" if completion else "live",
        "options": asdict(options) | {"public_env": list(options.public_env)},
        "store_path": "record",
        "cleanup_confirmed": False,
        "cleanup_errors": [],
        "error": None,
        "capture_boundaries": [],
        "task_success": "not_evaluated",
    }
    report_path = output / "report.json"
    report["preload"] = preload.describe()
    write_json(report_path, report)
    client = sandbox = store = agent = None

    def journal():
        with span("report.write"):
            write_json(report_path, report, replace=True)

    try:
        notify(ProgressEvent("connecting", task_id=binding.task.spec.task_id))
        with flow_scope("1.3", part="1.3.1"), span("sandbox.connect"):
            client = connect_docker(600)
        with flow_scope("1.3", part="1.3.2"), span("sandbox.configure"):
            sandbox = BtrfsSandbox(
                binding, preload=preload, client=client, network_mode=options.network,
            )
        report["container_name"] = sandbox.name
        report["sandbox_run_id"] = sandbox.run_id
        journal()
        notify(ProgressEvent("starting"))
        with flow_scope("1.3", part="1.3.3"), span("sandbox.start"):
            info = sandbox.start()
        report["preload"] = preload.describe()
        report["container_id"] = info.container_id
        report["image_id"] = info.actual_image_id
        journal()  # Resource identity survives interruption before C0.
        with flow_scope("1.4", part="1.4.1"):
            source = client.containers.get(info.container_id)
            keys = {value.split("=", 1)[0] for value in source.attrs["Config"].get("Env", [])}
            if set(options.public_env) - keys:
                raise RecordUsageError(
                    "requested public environment keys are absent from the container"
                )
            public_keys = (keys & PUBLIC_ENV_KEYS) | set(options.public_env)
            public_keys |= preload.base.metadata.launch.environment.keys()
            backend = BtrfsStateBackend(sandbox, public_env_keys=tuple(sorted(public_keys)))
        with flow_scope("1.4", part="1.4.2"), span("store.create"):
            store = RecordStore.create(output / "record")
            preload.attach(store)

        def capture(record_store, boundary, previous):
            report["phase"] = f"capturing_C{boundary.index}"
            journal()
            tick = time.monotonic()
            before = storage_metrics(record_store)["store_file_bytes"]
            state = backend.capture(record_store, boundary, previous)
            report["capture_boundaries"].append(
                {
                    "node_id": boundary.node_id,
                    "index": boundary.index,
                    "state_id": state.state_id,
                    "capture_sec": time.monotonic() - tick,
                    "store_bytes_after_capture": storage_metrics(record_store)["store_file_bytes"],
                    "store_bytes_before_capture": before,
                }
            )
            report["phase"] = "recording"
            journal()
            return state

        with flow_scope("1.4", part="1.4.3"):
            agent = MiniSweAgent(
                sandbox=sandbox,
                store=store,
                capture_state=capture,
                credentials=credentials,
                completion=completion,
                progress=notify,
                record_metadata={
                    "task_id": binding.task.spec.task_id,
                    "source": binding.task.spec.source.model_dump(mode="json"),
                    "repository": binding.task.spec.repository.model_dump(mode="json"),
                    "dataset_manifest_sha256": binding.dataset_manifest_sha256,
                    "images_manifest_sha256": binding.images_manifest_sha256,
                    "runner_options": report["options"],
                },
                retry_attempts=options.retry_attempts,
                model_config={
                    "model_name": model,
                    "cost_tracking": "ignore_errors",
                    "model_kwargs": {
                        "max_tokens": options.max_tokens,
                        "num_retries": 0,
                        "max_retries": 0,
                    },
                },
                agent_config={
                    "step_limit": options.max_steps,
                    "cost_limit": options.cost_limit,
                    "wall_time_limit_seconds": options.time_limit,
                },
            )
        report["phase"] = "recording"
        journal()
        tick = time.monotonic()
        try:
            with span("agent.run"):
                result = agent.run(AgentTask.from_package(binding.task))
        finally:
            report["agent_run_sec"] = time.monotonic() - tick
        with flow_scope("3.4", part="3.4.1"):
            report["agent_exit_status"] = result.get("exit_status")
            end = agent.recorder.end
            if end:
                timings = store.read_json(end.details).get("capture_timings", [])
                report["boundary_capture_commit_sec"] = sum(
                    row["duration_sec"]
                    for row in timings
                )
                report["boundary_metrics"] = boundary_summary(timings)
            report["run_id"] = agent.recorder.run.run_id
            report["run_end_status"] = end.status if end else "unfinalized"
            report["status"] = (
                "recorded" if end and end.status in ("completed", "paused") else "incomplete"
            )
            report["exit_code"] = 0 if report["status"] == "recorded" else 3
    except BaseException as error:
        record_phase("finalization")
        # Exception strings may contain provider URLs/credentials. Detailed
        # execution evidence belongs to the credential-aware adapter.
        report["error"] = {"type": type(error).__name__, "phase": report["phase"]}
        if isinstance(error, RecordUsageError):
            report["error"]["reason"] = str(error)
        report["status"] = "cancelled" if isinstance(error, KeyboardInterrupt) else "failed"
        report["exit_code"] = 130 if isinstance(error, KeyboardInterrupt) else 1
    finally:
        record_phase("finalization")
        tick = time.monotonic()
        notify(ProgressEvent("cleanup"))
        if sandbox:
            try:
                with flow_scope("3.2", part="3.2.1"), span("cleanup.sandbox"):
                    sandbox.close()
                report["cleanup_confirmed"] = True
            except BaseException as error:
                report["cleanup_errors"].append(
                    {"resource": "sandbox", "type": type(error).__name__}
                )
                if agent and agent.recorder and store:
                    try:
                        recorder = agent.recorder
                        store.save(
                            Failure(
                                run_id=recorder.run.run_id,
                                attempt_id=recorder.attempt.attempt_id,
                                category="cleanup",
                                details=store.put_json(
                                    {
                                        "phase": "runner-cleanup",
                                        "error_type": type(error).__name__,
                                        "container_id": report.get("container_id"),
                                    }
                                ),
                            )
                        )
                    except BaseException as diagnostic:
                        report["cleanup_errors"].append(
                            {"resource": "cleanup-diagnostic", "type": type(diagnostic).__name__}
                        )
        else:
            report["cleanup_confirmed"] = True
        if store:
            try:
                with flow_scope("3.2", part="3.2.2"), span("cleanup.store"):
                    store.close()
            except BaseException as error:
                report["cleanup_errors"].append({"resource": "store", "type": type(error).__name__})
        if client:
            try:
                with flow_scope("3.2", part="3.2.3"), span("cleanup.client"):
                    client.close()
            except BaseException as error:
                report["cleanup_errors"].append(
                    {"resource": "client", "type": type(error).__name__}
                )
        report["cleanup_sec"] = time.monotonic() - tick
        if report["cleanup_errors"]:
            report.update(status="failed", exit_code=1, cleanup_confirmed=False)
        with flow_scope("3.4", part="3.4.2"):
            # Inspect only AFTER closing the writer and disposing of the original container.
            if (output / "record" / "catalog.sqlite").exists():
                try:
                    notify(ProgressEvent("verifying"))
                    with flow_scope("3.3"), span("record.verify"):
                        view = inspect_record(
                            output / "record", check=True,
                        )
                    report["verification"] = view["verification"]
                    report["storage"] = view.get("storage")
                    committed = {n["node_id"] for run in view["runs"] for n in run["nodes"]}
                    for capture in report["capture_boundaries"]:
                        capture["node_committed"] = capture["node_id"] in committed
                    report["runs"] = [
                        {
                            key: run[key]
                            for key in (
                                "status",
                                "record_complete",
                                "incomplete_steps",
                                "unknown_executions",
                                "capture_metrics",
                            )
                        }
                        for run in view["runs"]
                    ]
                    if not view["verification"]["ok"]:
                        report.update(status="failed", exit_code=1)
                except BaseException as error:
                    report["verification_error"] = {"type": type(error).__name__}
                    report.update(status="failed", exit_code=1)
            report["duration_sec"] = time.monotonic() - started
            report["finished_at"] = datetime.now(UTC).isoformat()
            report["phase"] = "finished"
            journal()
    return report
