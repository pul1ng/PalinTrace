"""Initialize an independent recording at the actual restored boundary."""

from contextlib import nullcontext

from palinreplay.core.interfaces import AgentSnapshot, BoundaryAdapter, Payload
from palinreplay.core.models import State
from palinreplay.record.recorder import Recorder
from palinreplay.record.state import BtrfsStateBackend
from palinreplay.storage.btrfs.models import Snapshot
from palinreplay.storage.rootfs import SCOPE


def initial_recorder(
    store, materials, executor, export_agent=None, *, stats=None, continue_on_difference=False,
):
    if export_agent is None:
        initial = AgentSnapshot(Payload.json(materials.context), Payload.json(materials.input_position))
        def export_agent(boundary):
            return initial
    backend = BtrfsStateBackend(executor, public_env_keys=executor.runtime.launch.environment,
                                runtime=executor.runtime)
    config = {
        **materials.config,
        "replay": {
            "schema_version": "palinreplay.replay.v1",
            "source": materials.plan.provenance(),
            "used_history": [step.document() for step in materials.steps],
            "comparison_policy": "rootfs-files-v1-strict-mtime-seconds",
            "continue_on_difference": continue_on_difference,
        },
    }
    recorder = Recorder(store, task_id=materials.plan.run.task_id,
                        agent_name=materials.plan.run.agent_name,
                        agent_version=materials.plan.run.agent_version,
                        config=Payload.json(config),
                        boundary=BoundaryAdapter(export_agent, backend.capture))
    try:
        with stats.timed("snapshot_commit") if stats else nullcontext():
            node = recorder.start()
            state = store.get(State, node.state_id)
            snapshot = Snapshot.model_validate(store.read_json(state.manifest))
        source_snapshot = Snapshot.model_validate(executor.restored.report["snapshot"])
        # This explicit initial baseline is the prerequisite for subsequent deltas.
        with stats.timed("content_compare") if stats else nullcontext():
            executor.pool.command("verify", source=source_snapshot.name, workspace=snapshot.name,
                                  scope=SCOPE, oracle_sha256=executor.runtime.c0_oracle_sha256)
        return recorder
    except BaseException as error:
        recorder.__exit__(type(error), error, error.__traceback__)
        raise
