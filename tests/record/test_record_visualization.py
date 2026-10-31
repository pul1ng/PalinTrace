import hashlib
import importlib.util
import json
import sys
from itertools import pairwise
from pathlib import Path
from uuid import uuid4

import pytest

from palinreplay.cli import main
from palinreplay.core.models import StateCapture
from palinreplay.record.run import RunOptions, run_record
from palinreplay.storage.btrfs.pool import BtrfsPool
from palinreplay.storage.store import IntegrityError, RecordStore
from tests.agents.test_mini_agent import Capture, Transport, response
from tests.agents.test_mini_agent import deterministic as deterministic
from tests.record.test_record_run import runtime as runtime
from tests.sandbox.test_sandbox import package as package

SCRIPT = Path(__file__).resolve().parents[2] / "examples/render_record_flow.py"
spec = importlib.util.spec_from_file_location("record_viewer", SCRIPT)
viewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(viewer)


@pytest.fixture
def recorded(package, tmp_path, runtime, monkeypatch):
    import palinreplay.record.run as runner

    def build(*, storage=True):
        class Backend:
            def __init__(self, sandbox, **kwargs):
                self.capture_fixture = Capture(sandbox)

            def capture(self, store, boundary, previous):
                state = store.save(self.capture_fixture(store, boundary, previous))
                index = boundary.index
                details = {
                    "capture_mode": "btrfs-cow-snapshot",
                    "duration_sec": 0.0001,
                    "capture_complete": True,
                    "snapshot": {
                        "name": "bases/C0" if index == 0 else "checkpoints/" + boundary.node_id,
                        "uuid": str(uuid4()),
                        "location": {"volume": "fixture-pool", "uuid": str(uuid4())},
                    },
                }
                if storage:
                    details["storage"] = {
                        "image_allocated_bytes": 1_048_576 + index * 4096,
                        "image_capacity_bytes": 16 * 1024**3,
                    }
                store.save(StateCapture(
                    run_id=boundary.run_id, attempt_id=boundary.attempt_id,
                    step_id=boundary.step_id, node_id=boundary.node_id,
                    index=index, state_id=state.state_id, details=store.put_json(details),
                ))
                return state

        monkeypatch.setattr(runner, "BtrfsStateBackend", Backend)
        first = response("write", "bad")
        first.choices[0].message.content = '</script><script>alert("history")</script>'
        output = tmp_path / "run"
        report = run_record(
            package, output, completion=Transport([first, response("submit")]),
            options=RunOptions(preload="base", max_steps=2),
        )
        assert report["status"] == "recorded"
        return output, report

    return build


def files(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def test_btrfs_viewer_reads_nodes_calls_context_and_pool_measurements(recorded, monkeypatch):
    output, report = recorded()
    before = files(output)

    def forbidden(*args, **kwargs):
        pytest.fail("viewing must not mount a pool or run a full integrity check")

    monkeypatch.setattr(BtrfsPool, "__init__", forbidden)
    monkeypatch.setattr(RecordStore, "check_integrity", forbidden)
    data = viewer.build_data(output)
    assert files(output) == before
    assert data["counts"] == {"model_calls": 2, "tool_calls": 3}
    assert data["timeline_complete"]
    assert data["report_path"] == str(output / "report.json")
    assert data["total_sec"] == report["debug_timing"]["totals"]["record.total"]["duration_sec"]
    assert data["capture_total_sec"] == pytest.approx(
        report["debug_timing"]["totals"]["node.capture_commit"]["duration_sec"]
    )
    for index, node in enumerate(data["nodes"]):
        assert node["reuses_c0"] == (index == 0)
        assert node["image_allocated_bytes"] == 1_048_576 + index * 4096
        assert node["pool_delta_from_c0_bytes"] == index * 4096
        assert node["image_capacity_bytes"] == 16 * 1024**3
        assert node["context"]["messages"]
        assert "new_pack_bytes" not in node
    assert data["nodes"][0]["snapshot"]["name"] == "bases/C0"
    tools = [event for event in data["events"] if event["kind"] == "tool"]
    assert len(tools) == 3 and tools[1]["error"] and tools[1]["exit_code"] == 1
    assert all(event["duration"] is not None for event in tools)


def test_phase_intervals_and_groups_partition_the_recorded_timeline(recorded):
    output, _ = recorded()
    data = viewer.build_data(output)
    phases = data["phases"]
    assert phases[0]["start"] == 0
    for phase, following in pairwise(phases):
        assert phase["start"] + phase["duration"] == pytest.approx(following["start"])
    assert sum(phase["duration"] for phase in phases) == pytest.approx(data["total_sec"])
    assert phases[1]["groups"] == ["C0"] and phases[2]["groups"] == ["C1", "C2"]
    groups = [group for phase in phases for group in phase["groups"]]
    assert len(groups) == len(set(groups))
    assert set(groups) == {event["group"] for event in data["events"]}


@pytest.mark.parametrize("timing", ["missing", "capped"])
def test_missing_measurements_are_unknown_and_never_filled_with_zero(recorded, timing):
    output, report = recorded(storage=False)
    if timing == "missing":
        report.pop("debug_timing")
    else:
        report["debug_timing"]["timeline_complete"] = False
        report["debug_timing"]["spans"] = report["debug_timing"]["spans"][:2]
    (output / "report.json").write_text(json.dumps(report))
    data = viewer.build_data(output)
    assert not data["timeline_complete"]
    assert all(node["image_allocated_bytes"] is None for node in data["nodes"])
    assert all(node["pool_delta_from_c0_bytes"] is None for node in data["nodes"])
    assert all(event["duration"] is None for event in data["events"]
               if event["kind"] in ("model", "tool"))
    assert [node["duration"] for node in data["nodes"]] == [
        boundary["capture_sec"] for boundary in report["capture_boundaries"]
    ]


def test_html_embeds_evidence_safely_and_keeps_source_read_only(recorded, tmp_path, monkeypatch):
    output, _ = recorded()
    before = files(output)
    page = tmp_path / "viewer.html"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(output), "--output", str(page)])
    viewer.main()
    html = page.read_text()
    embedded = html.split('<script type="application/json" id="record-data">', 1)[1]
    embedded = embedded.split("</script>", 1)[0]
    data = json.loads(embedded)
    assert "</script>" not in embedded
    assert 'alert("history")' in str(data)
    assert [node["label"] for node in data["nodes"]] == ["C0", "C1", "C2"]
    assert "池镜像物理占用" in html and "不是单个快照的独占空间" in html
    assert "新增内容包" not in html and "增量巡检" not in html
    assert files(output) == before


@pytest.mark.parametrize("destination", ["default", "custom", "existing", "nested"])
def test_visualize_command_defaults_and_output_boundaries(
    recorded, tmp_path, monkeypatch, destination,
):
    import palinreplay.cli.outputs as outputs

    output, report = recorded()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(outputs, "timestamp", lambda: "261031_131022")
    before = files(output)
    page = tmp_path / "outputs/visual" / f"261031_131022_{report['task_id']}.html"
    args = ["record", "visualize", str(output)]
    if destination != "default":
        page = output / "record/new.html" if destination == "nested" else tmp_path / "custom.html"
        args += ["--output", str(page)]
    if destination == "existing":
        page.write_text("keep")
    code = main(args)
    assert files(output) == before
    if destination == "existing":
        assert code == 1 and page.read_text() == "keep"
    elif destination == "nested":
        assert code == 1 and not page.exists()
    else:
        assert code == 0 and '<script type="application/json"' in page.read_text()


def test_viewer_rejects_corrupt_capture_metadata(recorded):
    output, _ = recorded()
    with RecordStore.open(output / "record") as store:
        ref = store.list(StateCapture)[0].details
    artifact = output / "record/artifacts" / ref.sha256[:2] / ref.sha256[2:]
    artifact.chmod(0o600)
    artifact.write_bytes(artifact.read_bytes() + b"!")
    with pytest.raises(IntegrityError, match="size/hash mismatch"):
        viewer.build_data(output)
