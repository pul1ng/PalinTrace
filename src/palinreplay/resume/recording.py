"""Open a branch recording while borrowing the active Resume workspace."""

import base64
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256

from palinreplay.core.interfaces import BoundaryAdapter, Payload
from palinreplay.core.serialization import json_bytes
from palinreplay.record.recorder import Recorder
from palinreplay.record.state import BtrfsStateBackend
from palinreplay.storage.io import write_json
from palinreplay.storage.store import RecordStore


def branch_config(session):
    materials = session.materials
    source = {
        "store": str(session.start.source_store),
        "run": materials.run.model_dump(mode="json"),
        "node": materials.node.model_dump(mode="json"),
        "state": materials.state.model_dump(mode="json"),
    }
    # Old derivation permissions belong to the source Run, not this branch.
    config = {key: deepcopy(value) for key, value in materials.config.items()
              if key not in ("replay", "resume")}
    if "model" in config:
        config["model"]["model_name"] = session._decision_evidence["model"]
    config["resume"] = {
        "schema_version": "palinreplay.resume.v1",
        "initial_state_mode": "source-reference-v1",
        "node_numbering": "source-index-v1",
        "model": session.start.model, "max_steps": session.start.max_steps,
        "source": source, "source_sha256": sha256(json_bytes(source)).hexdigest(),
        "raw_materials": {role: base64.b64encode(getattr(materials, f"{role}_payload").data)
                          .decode("ascii") for role in ("config", "context", "input_position")},
    }
    return config


@contextmanager
def open_recording(session, export_agent):
    """Own the Store/Recorder only; session keeps ownership of executor and container."""
    with RecordStore.create(session.output / "record") as store:
        recorder = None
        try:
            materials = session.materials
            backend = BtrfsStateBackend(session.executor, runtime=materials.runtime,
                                       public_env_keys=materials.runtime.launch.environment)
            recorder = Recorder(
                store, task_id=materials.run.task_id, agent_name=materials.run.agent_name,
                agent_version=materials.run.agent_version, config=Payload.json(branch_config(session)),
                boundary=BoundaryAdapter(export_agent, backend.capture),
            )
            recorder.start_from_source()
            step = recorder.begin_step()
            session._record_attempt.update(status="recording", store=str(store.root),
                                           run_id=recorder.run.run_id, step_id=step.step.step_id,
                                           index=step.step.index)
            write_json(session.report_path, session.report, replace=True)
            yield recorder, step
        except BaseException as error:
            if recorder is not None:
                recorder.__exit__(type(error), error, error.__traceback__)
            raise
        else:
            recorder.close()
