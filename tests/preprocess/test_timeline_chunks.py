import hashlib

import pytest

from palinreplay.core.errors import RecordUsageError
from palinreplay.core.interfaces import CallAdapter, Payload, ResultCapture
from palinreplay.preprocess.chunks import (
    PromptTemplate,
    TiktokenCounter,
    make_token_counter,
    plan_chunks,
)
from palinreplay.preprocess.config import PreprocessConfig, PreprocessSettings
from palinreplay.preprocess.source import pin_source
from palinreplay.preprocess.timeline import (
    TimelineResult,
    TimelineSource,
    read_timeline,
)
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake


def request(command, timeout=60, cwd="/testbed"):
    return {
        "action": {"command": command},
        "interpreter": ["bash", "-c"],
        "cwd": cwd,
        "env": {"PATH": "/bin", "N": "1"},
        "timeout_sec": timeout,
    }


def adapter(output: bytes, returncode=0, name="mini.bash"):
    return CallAdapter(
        name=name,
        execute=lambda _: None,
        capture_request=Payload.json,
        capture_result=lambda _: ResultCapture(
            Payload(output, "application/octet-stream"),
            "fixture-merged-output-bytes",
            details={"decoded": {"returncode": returncode}},
            outcome="returned" if returncode == 0 else "error",
        ),
    )


def make_record(tmp_path, rows, final_role=None):
    root = tmp_path / "record"
    with RecordStore.create(root) as store:
        agent = fake.FakeAgent()
        with fake.recorder(store, agent, fake.FakeStateBackend()) as rec:
            step = rec.begin_step()
            model = step.model(fake.model_adapter(), {"tool_calls": [r[0] for r in rows]})
            response = model.invoke()
            fake.adopt(agent, model, response)
            step.declare_tools(model, tuple(r[0] for r in rows))
            for tool_id, tool_request, output, returncode in rows:
                call = step.tool(adapter(output, returncode), tool_request, tool_call_id=tool_id)
                value = call.invoke()
                fake.adopt(agent, call, {"role": "tool", "tool_call_id": tool_id, "value": value})
            if final_role:
                agent.messages.append(final_role)
            rec.commit_step()
            rec.finish()
        run_id = rec.run.run_id
    return root, run_id


def test_timeline_preserves_full_calls_order_and_final_tool_after_exit(tmp_path):
    root, run_id = make_record(
        tmp_path,
        [
            ("tool-a", request("first\nline"), b"one\n", 0),
            ("tool-b", request("second", timeout=2.5), b"\xff\x00binary", 7),
        ],
        final_role={"role": "exit", "content": "submitted"},
    )
    with RecordStore.open(root) as store:
        source = pin_source(store, run_id)
        timeline = read_timeline(store, source)
        assert timeline.source == TimelineSource.model_validate(source.document())
        assert len(timeline.calls) == 2
        first, second = timeline.calls
        assert (first.node_index, first.tool_ordinal) == (1, 1)
        assert (second.node_index, second.tool_ordinal) == (1, 2)
        assert first.tool_name == second.tool_name == "mini.bash"
        assert first.request.command == "first\nline"
        assert first.request.timeout_sec == 60
        assert second.request.timeout_sec == 2.5
        assert first.result == TimelineResult(outcome="returned", exit_code=0, output_text="one\n")
        assert second.result == TimelineResult(outcome="error", exit_code=7, output_text=None)
        assert first.refs.request.sha256 == hashlib.sha256(
            store.read_bytes(first.refs.request)
        ).hexdigest()
        assert first.refs.output.sha256 == hashlib.sha256(b"one\n").hexdigest()
        assert first.refs.details.sha256 == hashlib.sha256(
            store.read_bytes(first.refs.details)
        ).hexdigest()
        assert timeline.model_dump(mode="json")["calls"][1]["result"]["exit_code"] == 7


def test_empty_and_non_utf8_output_are_distinct(tmp_path):
    root, run_id = make_record(
        tmp_path,
        [("empty", request("true"), b"", 0), ("bin", request("false"), b"\x80", 1)],
    )
    with RecordStore.open(root) as store:
        timeline = read_timeline(store, pin_source(store, run_id))
    assert timeline.calls[0].result.output_text == ""
    assert timeline.calls[1].result.output_text is None


def test_unsupported_request_is_rejected(tmp_path):
    root, run_id = make_record(tmp_path, [("bad", {"command": "x"}, b"", 0)])
    with RecordStore.open(root) as store:
        with pytest.raises(RecordUsageError, match="unsupported tool request"):
            read_timeline(store, pin_source(store, run_id))


def test_chunks_keep_every_complete_call_once_and_retain_exact_fit(tmp_path):
    rows = [(f"t{i}", request(f"command-{i}"), f"output-{i}".encode(), 0) for i in range(5)]
    root, run_id = make_record(tmp_path, rows)
    with RecordStore.open(root) as store:
        timeline = read_timeline(store, pin_source(store, run_id))
    source = timeline.source.model_dump(mode="json")

    class CharCounter:
        name = "char"

        def count(self, text):
            return len(text)

    template = PromptTemplate(
        system="",
        user_prefix="",
        user_suffix="",
        response_token_reserve=0,
    )
    two = template.estimated_tokens(CharCounter(), source, timeline.calls[:2])
    template = PromptTemplate(
        system="",
        user_prefix="",
        user_suffix="",
        request_token_budget=two,
        response_token_reserve=0,
    )
    chunks = plan_chunks(timeline, CharCounter(), template)
    assert [len(chunk.calls) for chunk in chunks] == [2, 2, 1]
    assert [call for chunk in chunks for call in chunk.calls] == list(timeline.calls)
    assert all(chunk.estimated_tokens <= two for chunk in chunks)
    assert chunks[0].document()["first_node"] == 1


def test_one_oversized_call_fails_with_node_and_execution(tmp_path):
    root, run_id = make_record(tmp_path, [("huge", request("x" * 1000), b"", 0)])
    with RecordStore.open(root) as store:
        timeline = read_timeline(store, pin_source(store, run_id))

    class CharCounter:
        name = "char"

        def count(self, text):
            return len(text)

    template = PromptTemplate(
        system="", user_prefix="", user_suffix="", request_token_budget=50, response_token_reserve=0
    )
    with pytest.raises(RecordUsageError, match=r"C1 \(tool 1\).*exceeds"):
        plan_chunks(timeline, CharCounter(), template)


def test_empty_timeline_produces_no_chunks(tmp_path):
    root, run_id = make_record(tmp_path, [])
    with RecordStore.open(root) as store:
        timeline = read_timeline(store, pin_source(store, run_id))

    class CharCounter:
        name = "char"

        def count(self, text):
            return len(text)

    assert plan_chunks(timeline, CharCounter(), PromptTemplate(system="", user_prefix="")) == ()


def test_tokenizer_resolution_requires_an_explicit_mapping_for_unknown_models():
    settings = PreprocessSettings(tokenizer_model="gpt-4o")
    config = PreprocessConfig(
        llm={
            "base_url": "https://api.example.test/v1",
            "model": "custom-alias",
            "api_key": "fixture-key",
        },
        preprocess=settings,
    )
    counter = make_token_counter(config)
    assert isinstance(counter, TiktokenCounter) and counter.name

    bad_encoding = "definitely-not-a-real-encoding"
    config = PreprocessConfig(
        llm={
            "base_url": "https://api.example.test/v1",
            "model": "custom-alias",
            "api_key": "fixture-key",
        },
        preprocess=PreprocessSettings(tokenizer_encoding=bad_encoding),
    )
    with pytest.raises(RecordUsageError, match="tokenizer"):
        make_token_counter(config)
