import json

from palinreplay.core.interfaces import Payload
from palinreplay.resume import select_resume_start
from palinreplay.resume.context import ResumePrefix
from palinreplay.resume.materials import load_resume_materials
from palinreplay.storage.store import RecordStore
from tests.record.test_restore_materials import _record_run


def test_saved_prefix_is_lossless_and_new_settings_stay_separate(tmp_path, monkeypatch):
    monkeypatch.setattr("palinreplay.storage.btrfs.verification.verify_state", lambda *a: None)
    context = {
        "messages": [{"role": "user", "content": "保留\r\n"},
                     {"role": "tool", "content": "done", "tool_call_id": "tool-20"}],
        "config": {"model": {"model_name": "A"}, "agent": {"step_limit": 40}},
        "counters": {"n_calls": 20, "cost": 1.2, "consecutive_format_errors": 0},
        "clock": {"elapsed_seconds": 123}, "future_field": ["untouched"],
    }
    raw = json.dumps(context, ensure_ascii=False, indent=2).encode()
    position = {"next_message": 2, "adoptions": [{"start": 1, "stop": 2}]}
    with RecordStore.create(tmp_path / "source") as store:
        _, node, *_ = _record_run(store, "fixture", payloads={
            "context": Payload(raw, "application/json"),
            "input_position": Payload.json(position),
        })
    start = select_resume_start(tmp_path / "source", node.node_id, model="M", max_steps=2)
    materials = load_resume_materials(start)
    prefix = ResumePrefix(materials)
    assert prefix.context == context
    assert prefix.messages is materials.context["messages"]
    assert prefix.config is materials.context["config"]
    assert prefix.counters == context["counters"]
    assert prefix.input_position == position
    assert materials.context_payload.data == raw
    assert (start.model, start.max_steps) == ("M", 2)
    assert prefix.config["model"]["model_name"] == "A"
    assert prefix.config["agent"]["step_limit"] == 40
