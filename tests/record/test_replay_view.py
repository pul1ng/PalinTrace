import json

import pytest

from palinreplay.cli import main
from palinreplay.core.errors import IntegrityError, RecordUsageError
from palinreplay.core.models import Node
from palinreplay.record.inspect import export_record, inspect_record
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake
from tests.record.test_source_reference import configuration, recorder


@pytest.mark.parametrize("kind", ["completed", "empty", "tail"])
def test_replay_view_separates_source_from_actual_nodes(tmp_path, capsys, kind):
    config = configuration(tmp_path, end=20 if kind == "empty" else 21)
    root = tmp_path / "actual"
    agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
    with RecordStore.create(root) as store:
        rec = recorder(store, config, agent, backend)
        rec.start_from_source()
        if kind != "empty":
            step = fake.answer(rec, agent, ("one", "two") if kind == "tail" else ())
            if kind == "tail":
                call = step.tool(fake.tool_adapter(backend), {"action": "read"}, tool_call_id="one")
                fake.adopt(agent, call, call.invoke())
                rec.end_incomplete("exit mismatch")
            else:
                rec.commit_step()
        if kind != "tail":
            rec.finish()
        rec.close()
    view = inspect_record(root, check=True)
    run, = view["runs"]
    assert view["verification"]["ok"] and view["external_dependencies"][0]["checked"]
    assert run["source_start"]["index"] == 20 and run["source_start"]["reference"]
    assert run["record_complete"] is (kind != "tail")
    assert len(run["nodes"]) == (kind == "completed")
    assert bool(run["incomplete_steps"]) is (kind == "tail")
    assert run["status"] == ("incomplete" if kind == "tail" else "completed")
    assert main(["record", "inspect", str(root)]) == 0
    assert "Source C20 (reference)" in capsys.readouterr().out
    with pytest.raises(RecordUsageError, match="no committed node"):
        inspect_record(root, node="C20")
    if kind == "completed":
        selected = inspect_record(root, node="C21")["selected_node"]
        assert selected["index"] == 21 and selected["parent_node_id"] is None
        assert selected["context_content"]["value"]["messages"]
        assert selected["state"]["state_id"] and selected["calls"][0]["phase"] == "accepted"
        assert selected["node_id"] != config["replay"]["source"]["nodes"][-1]["node_id"]
    output = tmp_path / "export"
    manifest = export_record(root, output)
    assert manifest["external_dependencies"] and not manifest["self_contained"]
    trajectory = json.loads((output / "trajectory.json").read_text())
    assert not trajectory["restorable"]
    assert trajectory["external_dependencies"][0]["start"]["index"] == 20
    assert trajectory["runs"][0]["source_start"] == run["source_start"]
    with pytest.raises(RecordUsageError, match="external Replay source"):
        export_record(root, tmp_path / "bundle", bundle=True)
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("damage", ["missing-store", "missing-material"])
def test_checked_replay_view_and_export_reject_missing_source(tmp_path, damage):
    config = configuration(tmp_path, end=20)
    root = tmp_path / "actual"
    with RecordStore.create(root) as store:
        rec = recorder(store, config)
        rec.start_from_source()
        rec.finish()
        rec.close()
    source = tmp_path / "source"
    if damage == "missing-store":
        source.rename(tmp_path / "retained-source")
    else:
        with RecordStore.open(source) as origin:
            ref = origin.list(Node)[-1].context
            origin._artifacts._path(ref).unlink()
    unchecked = inspect_record(root)
    assert not unchecked["external_dependencies"][0]["checked"]
    checked = inspect_record(root, check=True)
    assert not checked["verification"]["ok"]
    assert "required Replay source unavailable" in str(checked["verification"]["issues"])
    with pytest.raises(IntegrityError, match="required Replay source unavailable"):
        export_record(root, tmp_path / "export")
    assert not (tmp_path / "export").exists()
