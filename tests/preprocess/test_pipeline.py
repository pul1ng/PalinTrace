import hashlib
import json
from types import SimpleNamespace

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


def adapter(output):
    return CallAdapter(
        name="mini.bash",
        execute=lambda _: None,
        capture_request=Payload.json,
        capture_result=lambda _: ResultCapture(
            Payload(output.encode(), "application/octet-stream"),
            "fixture-merged-output-bytes",
            details={"decoded": {"returncode": 0}},
        ),
    )


def make_record(tmp_path):
    root = tmp_path / "record"
    rows = [
        ("/srv/A/lifecycle.py start && /srv/A/request.py", "A_READY\n"),
        ("/srv/A/request.py", "A_OK\n"),
        ("/srv/A/lifecycle.py stop", "A_STOPPED\n"),
    ]
    with RecordStore.create(root) as store:
        agent = fake.FakeAgent()
        with fake.recorder(store, agent, fake.FakeStateBackend()) as rec:
            for index, (command, output) in enumerate(rows, 1):
                step = rec.begin_step()
                tool_id = f"tool-{index}"
                model = step.model(fake.model_adapter(), {"tool_calls": [tool_id]})
                response = model.invoke()
                fake.adopt(agent, model, response)
                step.declare_tools(model, (tool_id,))
                call = step.tool(adapter(output), request(command), tool_call_id=tool_id)
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
                {"action": "start", "observed_state": "up",
                 "request_quote": "/srv/A/lifecycle.py start",
                 "output_quote": "A_READY", "executable": True,
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
    assert manifest.schema_version == "palinreplay.preprocess-services.v1"
    assert [(s.name, s.start_inclusive, s.end_exclusive) for s in manifest.services] == [
        ("A", 1, 3)
    ]
    assert manifest.services[0].launch.start_command == "/srv/A/lifecycle.py start"
    assert manifest.services[0].launch.readiness_command == "/srv/A/request.py"
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
