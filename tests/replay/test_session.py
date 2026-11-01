import json

from palinreplay.replay.session import run_replay
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake


def test_preflight_failure_retains_report_without_starting_docker(tmp_path, monkeypatch):
    source = tmp_path / "source"
    with RecordStore.create(source) as store:
        agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
        with fake.recorder(store, agent, backend) as rec:
            rec.finish()
    def forbidden(*args):
        raise AssertionError("Docker must not start before material preflight")
    monkeypatch.setattr("palinreplay.replay.session.connect_docker", forbidden)
    output = tmp_path / "output"
    report = run_replay(source, "C0", output=output)
    assert report["status"] == "failed" and report["phase"] == "preflight"
    assert "version" in report["error"]
    assert report["cleanup_confirmed"]
    assert json.loads((output / "report.json").read_text()) == report
