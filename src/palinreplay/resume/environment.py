"""Create the writable R1 environment using the saved mini execution defaults."""

import math

from palinreplay.core.errors import IntegrityError
from palinreplay.record.restore import load_restore_materials, restore_r1_node
from palinreplay.restoration.environment import RestoredExecutor
from palinreplay.sandbox.client import docker_endpoint
from palinreplay.storage.store import RecordStore


def saved_execution_config(materials):
    config = materials.config["environment"]
    if config != materials.context["config"]["environment"]:
        raise IntegrityError("saved run and context execution configurations differ")
    cwd, env = config.get("cwd"), config.get("env")
    shell, timeout = config.get("interpreter"), config.get("timeout")
    if (not isinstance(cwd, str) or not cwd.startswith("/") or "\0" in cwd
            or not isinstance(env, dict)
            or any(not isinstance(k, str) or not k or "=" in k or "\0" in k
                   or not isinstance(v, str) or "\0" in v for k, v in env.items())
            or not isinstance(shell, list) or tuple(shell) != materials.runtime.launch.shell
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
            or config.get("forward_env", []) != []):
        raise IntegrityError("unsupported saved Resume execution configuration")
    return config


def restore_environment(start, materials, *, client, report_path):
    """Return owned resources and their borrowed adapter; caller must close both."""
    config = saved_execution_config(materials)
    restored = None
    try:
        with RecordStore.open(start.source_store, read_only=True) as store:
            with store.snapshot():
                current = load_restore_materials(store, start.node.node_id, run_id=start.node.run_id)
                if current != materials:
                    raise IntegrityError("Resume materials changed after preflight")
                restored = restore_r1_node(
                    store, start.node.node_id, run_id=start.node.run_id, client=client,
                    docker_host=docker_endpoint(), report_path=report_path,
                )
        return restored, RestoredExecutor(restored, materials.runtime, execution_config=config)
    except BaseException as error:
        if restored is None:
            raise  # The shared restorer owns cleanup and evidence during construction.
        restored.report.update(status="failed", phase="executor",
                               error_type=type(error).__name__, error_message=str(error))
        try:
            restored.close()
        except BaseException as cleanup:
            error.add_note(f"Resume environment cleanup also failed: {cleanup!r}")
        error.restore_report = restored.report
        raise
