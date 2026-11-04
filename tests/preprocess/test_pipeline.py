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
            outcome="error" if exit_code else "returned",
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
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps({"events": self.extract(payload)}), refusal=None,
                ),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    def extract(self, payload):
        events = []
        for call in payload["calls"]:
            command = call["request"]["command"]
            if "lifecycle.py start" in command:
                action, fragment, quote = "start", "/srv/A/lifecycle.py start", "A_STARTED"
            elif "lifecycle.py stop" in command:
                action, fragment, quote = "stop", "/srv/A/lifecycle.py stop", "A_STOPPED"
            else:
                continue
            events.append({
                "name": "A", "service_key": "/srv/A/lifecycle.py",
                "node_index": call["node_index"], "execution_id": call["execution_id"],
                "action": action, "command": fragment, "action_result": "succeeded",
                "output_quote": quote if quote in (call["result"]["output_text"] or "") else None,
            })
        return events

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
    client = FakeModels()
    result = run_preprocess(
        root, run_id=run_id, config=config, output=tmp_path / "out",
        client=client, counter=CharCounter(),
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
    assert report["schema_version"] == "palinreplay.preprocess-report.v2"
    assert report["extraction"]["event_count"] == 2
    assert report["extraction"]["model_requests"] == client.requests == 1
    assert report["timeline"]["call_count"] == 3
    assert "api_key" not in result.output.joinpath("report.json").read_text()
    assert digest_tree(root) == before


def single_pass_config(**overrides):
    return PreprocessConfig(
        llm={"base_url": "https://api.example.test/v1", "model": "single-pass-fixture",
             "api_key": "key"},
        preprocess={"request_token_budget": 100000, "response_token_reserve": 100,
                    "concurrency": 3, **overrides},
    )


def test_start_only_covers_last_committed_node_and_preserves_launch(tmp_path):
    rows = [("touch marker", "", 0) for _ in range(40)]
    rows[4] = ("/srv/A/lifecycle.py start", "", 0)
    root, run_id = make_record(tmp_path, rows)
    client = FakeModels()
    result = run_preprocess(
        root, run_id=run_id, config=single_pass_config(), client=client,
        counter=CharCounter(), output=tmp_path / "out",
    )
    service, = result.manifest.services
    assert (service.start_inclusive, service.end_exclusive) == (5, 41)
    assert service.evidence.stop is None
    assert service.launch.start_command == rows[4][0]
    assert select_services(result.manifest, 4) == ()
    for node in (5, 20, 40):
        assert [s.name for s in select_services(result.manifest, node)] == ["A"]
    assert client.requests == 1


def test_successful_stop_without_start_reports_source_and_publishes_no_manifest(tmp_path):
    root, run_id = make_record(tmp_path, [("/srv/A/lifecycle.py stop", "A_STOPPED\n")])
    before = digest_tree(root)
    with pytest.raises(ValueError, match=r"A.*C1/.*stop.*no matching recorded start"):
        run_preprocess(
            root, run_id=run_id, config=single_pass_config(), client=FakeModels(),
            counter=CharCounter(), output=tmp_path / "out",
        )
    assert not (tmp_path / "out" / "services.json").exists()
    assert digest_tree(root) == before


def test_original_fragment_can_retain_its_surrounding_whitespace(tmp_path):
    root, run_id = make_record(tmp_path, [("  /srv/A/lifecycle.py start  ", "", 0)])

    class SpacedModels(FakeModels):
        def extract(self, payload):
            events = super().extract(payload)
            events[0]["command"] = payload["calls"][0]["request"]["command"]
            return events

    result = run_preprocess(
        root, run_id=run_id, config=single_pass_config(), client=SpacedModels(),
        counter=CharCounter(), output=tmp_path / "out",
    )
    assert result.manifest.services[0].launch.start_command == "/srv/A/lifecycle.py start"


def test_controls_in_one_call_use_fragment_order_and_share_the_service_key(tmp_path):
    root, run_id = make_record(tmp_path, [
        ("/srv/A/lifecycle.py start && /srv/A/lifecycle.py stop", "A_STARTED\nA_STOPPED\n", 0),
    ])

    class MultipleModels(FakeModels):
        def extract(self, payload):
            start, = super().extract(payload)
            stop = {**start, "action": "stop", "command": "/srv/A/lifecycle.py stop",
                    "output_quote": "A_STOPPED"}
            return [stop, start]

    result = run_preprocess(
        root, run_id=run_id, config=single_pass_config(), client=MultipleModels(),
        counter=CharCounter(), output=tmp_path / "out",
    )
    assert [e["action"] for e in result.report["extraction"]["events"]] == ["start", "stop"]
    assert len(result.report["extraction"]["services"]) == 1
    assert result.manifest.services == ()


def test_empty_record_makes_zero_requests_and_has_an_empty_manifest(tmp_path):
    root, run_id = make_record(tmp_path, [])
    client = FakeModels()
    result = run_preprocess(
        root, run_id=run_id, config=single_pass_config(), client=client,
        counter=CharCounter(), output=tmp_path / "out",
    )
    assert result.manifest.services == ()
    assert result.report["extraction"]["model_requests"] == client.requests == 0


def test_cross_chunk_stop_arrives_first_and_display_names_can_differ(tmp_path):
    import threading

    from palinreplay.preprocess.extraction import extraction_template
    from palinreplay.preprocess.source import pin_source
    from palinreplay.preprocess.timeline import read_timeline

    root, run_id = make_record(tmp_path)
    with RecordStore.open(root) as store:
        timeline = read_timeline(store, pin_source(store, run_id))
    template = extraction_template(response_token_reserve=100)
    source = timeline.source.model_dump(mode="json")
    budget = max(template.estimated_tokens(CharCounter(), source, (c,))
                 for c in timeline.calls)

    class ReverseModels(FakeModels):
        def __init__(self):
            super().__init__()
            self.stop_returned = threading.Event()
            self.returned = []

        def extract(self, payload):
            call, = payload["calls"]
            node = call["node_index"]
            if node == 1:
                assert self.stop_returned.wait(5)
            events = super().extract(payload)
            if node == 3:
                events[0]["name"] = "service-A"
            self.returned.append(node)
            if node == 3:
                self.stop_returned.set()
            return events

    client = ReverseModels()
    result = run_preprocess(
        root, run_id=run_id, config=single_pass_config(request_token_budget=budget),
        client=client, counter=CharCounter(), output=tmp_path / "out",
    )
    assert len(result.report["timeline"]["chunks"]) == client.requests == 3
    assert client.returned.index(3) < client.returned.index(1)
    assert [event["node_index"] for event in result.report["extraction"]["events"]] == [1, 3]
    service, = result.manifest.services
    assert (service.name, service.start_inclusive, service.end_exclusive) == ("A", 1, 3)
    assert result.report["extraction"]["services"][0]["aliases"] == ["A", "service-A"]


@pytest.mark.parametrize("output,code,expected_end,expected_result", [
    ("A_STOPPED\n", 1, 2, "succeeded"),
    ("", 1, 4, "unattributed"),
])
def test_compound_stop_uses_only_attributable_recorded_receipt(
    tmp_path, output, code, expected_end, expected_result,
):
    root, run_id = make_record(tmp_path, [
        ("/srv/A/lifecycle.py start", "", 0),
        ("/srv/A/lifecycle.py stop && false", output, code),
        ("touch tail", "", 0),
    ])
    result = run_preprocess(
        root, run_id=run_id, config=single_pass_config(), client=FakeModels(),
        counter=CharCounter(), output=tmp_path / "out",
    )
    assert result.manifest.services[0].end_exclusive == expected_end
    item = result.report["extraction"]["services"][0]["items"][1]["events"][0]
    assert item["action_result"] == expected_result
    assert item["call_exit_code"] == 1 and item["exit_code"] is None


def test_failed_stop_preserves_open_interval(tmp_path):
    root, run_id = make_record(tmp_path, [
        ("/srv/A/lifecycle.py start", "", 0),
        ("/srv/A/lifecycle.py stop", "A_STOP_FAILED\n", 9),
        ("touch tail", "", 0),
    ])
    result = run_preprocess(
        root, run_id=run_id, config=single_pass_config(), client=FakeModels(),
        counter=CharCounter(), output=tmp_path / "out",
    )
    service, = result.manifest.services
    assert service.end_exclusive == 4 and service.evidence.stop is None


@pytest.mark.parametrize("change,pattern", [
    ("call", "does not identify a call"),
    ("node", "does not identify a call"),
    ("fragment", "complete original shell fragment"),
    ("key", "service_key must occur"),
    ("probe", "start.*stop"),
    ("duplicate", "duplicate control fragment"),
])
def test_invalid_single_pass_output_never_publishes_a_manifest(tmp_path, change, pattern):
    root, run_id = make_record(tmp_path)

    class InvalidModels(FakeModels):
        def extract(self, payload):
            events = super().extract(payload)
            first = next((e for e in events if e["action"] == "start"), None)
            if first is None:
                return events
            if change == "call":
                first["execution_id"] = "foreign"
            elif change == "node":
                first["node_index"] = 99
            elif change == "fragment":
                first["command"] += " --fabricated"
            elif change == "key":
                first["service_key"] = "/srv/B/lifecycle.py"
            elif change == "probe":
                first["action"] = "probe"
            elif change == "duplicate":
                events.append(dict(first))
            return events

    with pytest.raises((ValueError, RuntimeError), match=pattern):
        run_preprocess(
            root, run_id=run_id, config=single_pass_config(), client=InvalidModels(),
            counter=CharCounter(), output=tmp_path / "out",
        )
    assert not (tmp_path / "out" / "services.json").exists()



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
    """Fixed single-pass extraction responses for offline Record fixtures."""

    def __init__(self, start_fragment):
        super().__init__()
        self.start_fragment = start_fragment

    def extract(self, payload):
        events = []
        for call in payload["calls"]:
            command = call["request"]["command"]
            if "start-A.sh" not in command:
                continue
            action = "stop" if "--stop" in command else "start"
            events.append({
                "name": "A", "service_key": "./start-A.sh",
                "node_index": call["node_index"], "execution_id": call["execution_id"],
                "action": action,
                "command": command if action == "stop" else self.start_fragment,
            })
        return events

@pytest.mark.parametrize("first,output,code,launch,stop", [
    ("./start-A.sh >a.log 2>&1 &", "", 0, "./start-A.sh >a.log 2>&1 &", False),
    ("./start-A.sh --fail", "A_LAUNCH_FAILED\n", 17, "./start-A.sh --fail", False),
    ("./start-A.sh && false", "A_STARTED\n", 1, "./start-A.sh", False),
    ("./start-A.sh || echo A_STARTED", "A_STARTED\n", 0, "./start-A.sh", False),
    ("./start-A.sh", "", 0, "./start-A.sh", True),
    ("touch first.txt", "", 0, None, False),
], ids=["start", "failed", "and-false", "or-echo", "stop", "files"])
def test_binary_record_pipeline_and_replay_adapter_without_probes(tmp_path, first, output, code, launch, stop):
    from palinreplay.core.models import Node
    from palinreplay.replay.plan import pin_replay
    from palinreplay.replay.service_config import load_service_input, resolve_services

    rows = [
        (first, output, code),
        ("cp first.txt second.txt", "", 0),
        ("./start-A.sh --stop" if stop else "touch last.txt", "", 0),
    ]
    root, run_id = make_record(tmp_path, rows=rows)
    before = digest_tree(root)
    result = run_preprocess(root, run_id=run_id, output=tmp_path / "out", client=BinaryModels(launch),
        counter=CharCounter(), config=PreprocessConfig(
            llm={"base_url": "https://api.example.test/v1", "model": "fixed-binary-fixture", "api_key": "key"},
            preprocess={"request_token_budget": 100000, "response_token_reserve": 1000}))
    path = result.output / "services.json"
    document = json.loads(path.read_text())
    assert "unknowns" not in document
    expected = [("A", 1, 3 if stop else 4)] if launch else []
    assert [(s.name, s.start_inclusive, s.end_exclusive) for s in result.manifest.services] == expected
    for item in document["services"]:
        assert item["launch"]["start_command"] == launch
        assert "readiness_command" not in item["launch"]
        assert "probe" not in item["evidence"]
        start = result.report["lifecycles"][0]["intervals"][0]["start"]
        assert start["call_exit_code"] == code
        if first != launch:
            assert start["exit_code"] is None
        elif code != 0:
            assert start["exit_code"] == code and start["action_result"] == "failed"
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
