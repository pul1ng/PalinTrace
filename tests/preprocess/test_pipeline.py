import hashlib
import json
from types import SimpleNamespace

import pytest

from palinreplay.core.interfaces import CallAdapter, Payload, ResultCapture
from palinreplay.preprocess.config import PreprocessConfig, PreprocessSettings
from palinreplay.preprocess.manifest import load_manifest, select_services
from palinreplay.preprocess.pipeline import run_preprocess
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake


def request(command):
    return {
        "action": {"command": command},
        "interpreter": ["bash", "-c"],
        "cwd": "/testbed",
        "env": {},
        "timeout_sec": 60,
    }


def adapter(output, exit_code=0):
    return CallAdapter(
        name="mini.bash",
        execute=lambda _: None,
        capture_request=Payload.json,
        capture_result=lambda _: ResultCapture(
            Payload(output.encode(), "application/octet-stream"),
            "fixture-merged-output-bytes",
            details={"decoded": {"returncode": exit_code}},
        ),
    )


def make_record(tmp_path, rows=None):
    root = tmp_path / "record"
    rows = rows if rows is not None else [
        ("/srv/A/lifecycle.py start && /srv/A/request.py", "A_STARTED\nA_READY\n"),
        ("/srv/A/request.py", "A_OK\n"),
        ("/srv/A/lifecycle.py stop", "A_STOPPED\n"),
    ]
    with RecordStore.create(root) as store:
        agent = fake.FakeAgent()
        with fake.recorder(store, agent, fake.FakeStateBackend()) as rec:
            for index, row in enumerate(rows, 1):
                command, output = row[:2]
                exit_code = row[2] if len(row) == 3 else 0
                step = rec.begin_step()
                tool_id = f"tool-{index}"
                model = step.model(fake.model_adapter(), {"tool_calls": [tool_id]})
                response = model.invoke()
                fake.adopt(agent, model, response)
                step.declare_tools(model, (tool_id,))
                call = step.tool(adapter(output, exit_code), request(command), tool_call_id=tool_id)
                value = call.invoke()
                fake.adopt(agent, call, {"role": "tool", "tool_call_id": tool_id, "value": value})
                rec.commit_step()
            rec.finish()
        run_id = rec.run.run_id
    return root, run_id


class FakeModels:
    def __init__(self):
        self.requests = 0

    def complete(self, messages, **parameters):
        self.requests += 1
        payload = json.loads(messages[1]["content"].split("\n\n", 1)[1])
        if "calls" in payload:
            content = self.candidate(payload)
        else:
            content = self.evidence(payload)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(content), refusal=None),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    @staticmethod
    def candidate(payload):
        candidates = []
        for call in payload["calls"]:
            command = call["request"]["command"]
            actions = ["start", "probe"] if " start" in command else (
                ["stop"] if " stop" in command else ["probe"]
            )
            state = "down" if " stop" in command else "up"
            candidates.append({
                "name": "A", "node_index": call["node_index"],
                "execution_id": call["execution_id"],
                "actions": actions, "candidate_state": state,
            })
        return {"candidates": candidates}

    @staticmethod
    def evidence(payload):
        candidate = payload["candidate"]
        call = payload["call"]
        command = call["request"]["command"]
        identity = [{
            "kind": "program", "value": "/srv/A",
            "quote": "/srv/A", "field": "request.command",
        }]
        if "lifecycle.py start" in command:
            events = [
                {"action": "start", "observed_state": "unknown", "action_result": "succeeded",
                 "request_quote": "/srv/A/lifecycle.py start",
                 "output_quote": "A_STARTED", "executable": True,
                 "dependencies": [], "reason": "started"},
                {"action": "probe", "observed_state": "up",
                 "request_quote": "/srv/A/request.py",
                 "output_quote": "A_READY", "executable": True,
                 "dependencies": [], "reason": "ready"},
            ]
        elif " stop" in command:
            events = [{
                "action": "stop", "observed_state": "down",
                "request_quote": "/srv/A/lifecycle.py stop",
                "output_quote": "A_STOPPED", "executable": True,
                "dependencies": [], "reason": "stopped",
            }]
        else:
            events = [{
                "action": "probe", "observed_state": "up",
                "request_quote": "/srv/A/request.py",
                "output_quote": "A_OK", "executable": True,
                "dependencies": [], "reason": "responded",
            }]
        return {
            "service_name": candidate["name"],
            "identity": identity,
            "events": events,
            "unknowns": [],
        }


class CharCounter:
    name = "char"

    def count(self, text):
        return len(text)


def digest_tree(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def test_end_to_end_pipeline_writes_manifest_and_report(tmp_path):
    root, run_id = make_record(tmp_path)
    before = digest_tree(root)
    config = PreprocessConfig(
        llm={"base_url": "https://api.example.test/v1", "model": "fixture", "api_key": "key"},
        preprocess=PreprocessSettings(
            request_token_budget=100000, response_token_reserve=1000, concurrency=2
        ),
    )
    result = run_preprocess(
        root, run_id=run_id, config=config, output=tmp_path / "out",
        client=FakeModels(), counter=CharCounter(),
    )
    manifest = load_manifest(result.output / "services.json")
    assert manifest.schema_version == "palinreplay.preprocess-services.v2"
    assert [(s.name, s.start_inclusive, s.end_exclusive) for s in manifest.services] == [
        ("A", 1, 3)
    ]
    assert manifest.services[0].launch.start_command == "/srv/A/lifecycle.py start"
    assert manifest.services[0].launch.readiness_command is None
    assert manifest.unknowns == ()
    assert [s.name for s in select_services(manifest, 1)] == ["A"]
    assert select_services(manifest, 3) == ()
    report = json.loads((result.output / "report.json").read_text())
    assert report["model"]["model"] == "fixture"
    assert report["candidates"]["count"] == 3
    assert report["timeline"]["call_count"] == 3
    assert "api_key" not in result.output.joinpath("report.json").read_text()
    assert digest_tree(root) == before


def test_failed_analysis_leaves_no_services_file(tmp_path):
    root, run_id = make_record(tmp_path)
    config = PreprocessConfig(
        llm={"base_url": "https://api.example.test/v1", "model": "fixture", "api_key": "key"},
        preprocess=PreprocessSettings(request_token_budget=100000, response_token_reserve=1000),
    )
    with __import__("pytest").raises(RuntimeError):
        run_preprocess(
            root, run_id=run_id, config=config, output=tmp_path / "out",
            client=type("Broken", (), {"complete": lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("model down")
            )})(),
            counter=CharCounter(),
        )
    assert not (tmp_path / "out" / "services.json").exists()


class BinaryModels(FakeModels):
    """Fixed responses for offline Record fixtures; not live recognition evidence."""

    @staticmethod
    def candidate(payload):
        candidates = []
        for call in payload["calls"]:
            command = call["request"]["command"]
            if "start-A.sh" not in command:
                continue
            action = "stop" if "--stop" in command else "start"
            candidates.append({"name": "A", "node_index": call["node_index"],
                "execution_id": call["execution_id"], "actions": [action],
                "candidate_state": "unknown"})
        return {"candidates": candidates}

    @staticmethod
    def evidence(payload):
        call = payload["call"]
        command = call["request"]["command"]
        action = "stop" if "--stop" in command else "start"
        return {"service_name": "A", "identity": [{
            "kind": "program", "value": "./start-A.sh", "quote": "./start-A.sh",
            "field": "request.command"}], "events": [{
                "action": action, "observed_state": "unknown", "request_quote": command,
                "output_quote": None, "executable": True, "dependencies": [],
                "reason": "fixed launch candidate; code checks actual source exit"}], "unknowns": []}


@pytest.mark.parametrize("case,expected", [
    ("start", [("A", 1, 4)]), ("failed", []), ("stop", [("A", 1, 3)]), ("files", []),
])
def test_binary_record_pipeline_and_replay_adapter_without_probes(tmp_path, case, expected):
    from palinreplay.core.models import Node
    from palinreplay.replay.plan import pin_replay
    from palinreplay.replay.service_config import load_service_input, resolve_services

    rows = [
        ({"files": "touch first.txt", "failed": "./start-A.sh --fail"}.get(
            case, "./start-A.sh >a.log 2>&1 &"), "", 17 if case == "failed" else 0),
        ("cp first.txt second.txt", "", 0),
        ("./start-A.sh --stop" if case == "stop" else "touch last.txt", "", 0),
    ]
    root, run_id = make_record(tmp_path, rows=rows)
    before = digest_tree(root)
    result = run_preprocess(root, run_id=run_id, output=tmp_path / "out", client=BinaryModels(),
        counter=CharCounter(), config=PreprocessConfig(
            llm={"base_url": "https://api.example.test/v1", "model": "fixed-binary-fixture", "api_key": "key"},
            preprocess={"request_token_budget": 100000, "response_token_reserve": 1000}))
    path = result.output / "services.json"
    document = json.loads(path.read_text())
    assert "unknowns" not in document
    assert [(s.name, s.start_inclusive, s.end_exclusive) for s in result.manifest.services] == expected
    for item in document["services"]:
        assert item["launch"] is not None and "readiness_command" not in item["launch"]
        assert "probe" not in item["evidence"]
    with RecordStore.open(root) as store:
        nodes = sorted(store.list(Node), key=lambda n: n.index)
        for node in nodes:
            selected = resolve_services(load_service_input(path), store,
                                        pin_replay(store, f"C{node.index}", "C3"))
            active = bool(expected and expected[0][1] <= node.index < expected[0][2])
            assert [s.name for s in selected.services] == (["A"] if active else [])
        if expected:
            assert result.manifest.services[0].evidence.start.execution_id == nodes[1].tool_execution_ids[0]
    assert digest_tree(root) == before
