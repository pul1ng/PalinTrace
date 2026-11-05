"""Public entry point for the pinned mini adapter (optional ``mini`` extra)."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import tempfile
import threading
from collections.abc import Callable
from copy import deepcopy

from palinreplay.agents import AgentCapabilities, AgentInfo, AgentTask
from palinreplay.core.interfaces import AgentSnapshot
from palinreplay.core.progress import ProgressCallback, ProgressReporter
from palinreplay.diagnostics.metrics import timed
from palinreplay.diagnostics.record_timing import flow_scope
from palinreplay.llm import LLMConfig
from palinreplay.sandbox.base import Sandbox
from palinreplay.storage.store import RecordStore

MINI_VERSION = "2.4.6"
MINI_COMMIT = "04d809ceab9df28f9adaed044884180159172930"
_import_lock = threading.Lock()
_native = None


def load_native():
    """Verify provenance and suppress mini's implicit user-global dotenv loading.

    No environment override survives import. The native query function gets
    per-instance statistics; we never replace mini's module-global counters.
    """
    global _native
    with _import_lock:
        if _native is not None:
            return _native
        try:
            distribution = importlib.metadata.distribution("mini-swe-agent")
        except importlib.metadata.PackageNotFoundError as error:
            raise ImportError(
                "install PalinReplay's pinned optional dependency: uv sync --extra mini"
            ) from error
        origin = json.loads(distribution.read_text("direct_url.json") or "{}")
        if (
            distribution.version != MINI_VERSION
            or origin.get("vcs_info", {}).get("commit_id") != MINI_COMMIT
        ):
            raise RuntimeError(
                f"mini adapter requires mini-swe-agent {MINI_VERSION} at {MINI_COMMIT}"
            )
        with tempfile.TemporaryDirectory(prefix="palin-mini-config-") as directory:
            overrides = {"MSWEA_GLOBAL_CONFIG_DIR": directory, "MSWEA_SILENT_STARTUP": "1"}
            previous = {key: os.environ.get(key) for key in overrides}
            try:
                os.environ.update(overrides)
                _native = importlib.import_module("palinreplay.agents._mini_native")
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        return _native


def prepare_decision_request(messages, model_config):
    """Prepare the pinned model's public input without starting its Agent loop."""
    return load_native().prepare_decision_request(messages, model_config)


def complete_decision_request(request, credentials):
    """Issue one prepared model call, independently of the recording Agent."""
    return load_native().complete_decision_request(request, credentials)


def swebench_config() -> dict:
    """Resolved public templates from the pinned upstream package."""
    load_native()
    import minisweagent
    import yaml

    return yaml.safe_load(
        (minisweagent.package_dir / "config/benchmarks/swebench.yaml").read_text()
    )


class MiniSweAgent:
    """One run per object. Store and started Sandbox remain caller-owned.

    ``completion`` is an optional SDK-compatible transport for deterministic
    verification; parsing, formatting, budgets and the Agent loop stay native.
    Credentials are passed privately, never placed in mini's public config.
    """

    info = AgentInfo(
        "mini-swe-agent",
        f"{MINI_VERSION}+{MINI_COMMIT}",
        AgentCapabilities(run=True, trajectory_export=True, record=True),
    )

    @timed("agent.configure")
    def __init__(
        self,
        *,
        sandbox: Sandbox,
        store: RecordStore,
        capture_state: Callable,
        model_config: dict,
        agent_config: dict | None = None,
        environment_config: dict | None = None,
        credentials: LLMConfig | None = None,
        retry_attempts: int = 10,
        completion: Callable | None = None,
        record_metadata: dict | None = None,
        progress: ProgressCallback | None = None,
    ):
        native = load_native()
        defaults = swebench_config()
        self._store, self._sandbox, self._capture_state = store, sandbox, capture_state
        self._record_metadata = deepcopy(record_metadata or {})
        self._progress = ProgressReporter(progress)
        native.public_config(self._record_metadata)
        self._agent_config = deepcopy(defaults["agent"] | (agent_config or {}))
        self._env_config = deepcopy(
            {
                "timeout": defaults["environment"]["timeout"],
                "env": defaults["environment"]["env"],
                **(environment_config or {}),
            }
        )
        self._model = native.ObservedModel(
            **(defaults["model"] | model_config),
            credentials=credentials,
            retry_attempts=retry_attempts,
            completion=completion,
        )
        self._agent = None

    @property
    def recorder(self):
        return self._agent.recorder if self._agent is not None else None

    @property
    def native_agent(self):
        """Inspection only; driving it outside run() is unsupported."""
        return self._agent

    def run(self, task: AgentTask) -> dict:
        if self._agent is not None:
            raise RuntimeError("create a fresh mini adapter for each run")
        native = load_native()
        with flow_scope("1.4", part="1.4.4"):
            env = native.BorrowedEnvironment(self._sandbox, **self._env_config)
            self._agent = native.RecordingAgent(
                self._model,
                env,
                store=self._store,
                task=task,
                info=self.info,
                capture_state=self._capture_state,
                record_metadata=self._record_metadata,
                progress=self._progress,
                **self._agent_config,
            )
        return self._agent.run(task.instruction, **deepcopy(task.public_context))

    def export_snapshot(self) -> AgentSnapshot:
        if self._agent is None:
            raise RuntimeError("run has not initialized the Agent")
        return self._agent.export_snapshot()
