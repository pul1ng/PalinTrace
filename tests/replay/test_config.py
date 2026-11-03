import pytest

from palinreplay.agents import AgentTask
from palinreplay.replay.config import replay_config
from palinreplay.replay.materials import load_replay_materials
from palinreplay.replay.plan import pin_replay
from palinreplay.replay.services import PreparationError
from tests.agents.test_mini_agent import deterministic, integrated, response  # noqa: F401


def test_v2_configuration_pins_c20_and_only_c21_through_c40(tmp_path):
    source, _, _, _, agent = integrated(
        tmp_path, [response("read")] * 40 + [response("submit")],
        agent_config={"step_limit": 50},
    )
    try:
        agent.run(AgentTask("task", "Task"))
        materials = load_replay_materials(source, pin_replay(source, "C20", "C40"))
        prepared = {"status": "ready", "config": {"services": [{"name": "A"}]}, "services": []}
        config = replay_config(materials, preparation=prepared)
        replay = config["replay"]
        assert replay["schema_version"] == "palinreplay.replay.v2"
        assert replay["initial_state_mode"] == "source-reference-v1"
        assert replay["node_numbering"] == "source-index-v1"
        assert replay["execution_policy"] == "historical-exit-code-v1"
        assert (replay["source_start_index"], replay["source_end_index"]) == (20, 40)
        assert [s["index"] for s in replay["used_history"]] == list(range(21, 41))
        assert replay["source"] == materials.plan.provenance()
        start = replay["source_start"]
        assert start["node_id"] == materials.plan.start.node_id
        assert start["state_id"] == materials.plan.states[0].state_id
        assert start["context"] == source.read_json(materials.plan.start.context)
        assert start["input_position"] == source.read_json(materials.plan.start.input_position)
        assert start["context"] != source.read_json(materials.plan.end.context)
        assert config["environment"] == materials.config["environment"]
        assert replay["preparation"] == {"config": prepared["config"], "evidence": prepared}
        assert not replay["preparation_state_captured"]
        assert "validation_mode" not in replay and "comparison_policy" not in replay
        assert "replay" not in materials.config
        # The selected endpoint stays at C40 although the source also contains C41.
        assert materials.plan.end.index == 40
    finally:
        source.close()


def test_configuration_rejects_incomplete_preparation():
    with pytest.raises(PreparationError, match="must be ready"):
        replay_config(None, preparation={"status": "failed"})
