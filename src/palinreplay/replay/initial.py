"""Initialize an independent recording at the actual restored boundary."""

from palinreplay.core.interfaces import AgentSnapshot, BoundaryAdapter, Payload
from palinreplay.record.recorder import Recorder
from palinreplay.record.state import BtrfsStateBackend
from palinreplay.replay.config import replay_config
from palinreplay.restoration.validation import validate_r1 as validate_r1


def initial_recorder(
    store, materials, executor, export_agent=None, *, r1=None, preparation=None,
):
    config = replay_config(materials, r1=r1, preparation=preparation)
    if export_agent is None:
        initial = AgentSnapshot(Payload.json(materials.context), Payload.json(materials.input_position))
        def export_agent(boundary):
            return initial
    backend = BtrfsStateBackend(executor, public_env_keys=executor.runtime.launch.environment,
                                runtime=executor.runtime)
    recorder = Recorder(store, task_id=materials.plan.run.task_id,
                        agent_name=materials.plan.run.agent_name,
                        agent_version=materials.plan.run.agent_version,
                        config=Payload.json(config),
                        boundary=BoundaryAdapter(export_agent, backend.capture))
    try:
        recorder.start_from_source()
        return recorder
    except BaseException as error:
        recorder.__exit__(type(error), error, error.__traceback__)
        raise
