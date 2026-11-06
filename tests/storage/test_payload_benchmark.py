"""Independent payload roundtrip and benchmark accounting checks."""

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from palinreplay.core.interfaces import Payload
from palinreplay.core.serialization import json_bytes
from palinreplay.diagnostics.metrics import measure
from palinreplay.storage.store import RecordStore

BENCHMARK_PATH = Path(__file__).parents[2] / "scripts" / "benchmarks" / "record_payloads.py"
SPEC = importlib.util.spec_from_file_location("record_payload_benchmark", BENCHMARK_PATH)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


@pytest.mark.parametrize("payload", [
    Payload(b"", "application/octet-stream"),
    Payload(bytes(range(256)) * 32, "application/octet-stream"),
    Payload(b'{ "messages" : [{"role":"user", "content":"space"}] }\n', "application/json"),
    Payload(b'{"key":1,"key":2}', "application/json"),
    Payload(b'{"key":-0.0,"value":1e+02}', "application/json"),
    Payload(b'{"key":"\\u4f60\\u597d"}', "application/json"),
    Payload(b'{"bad":NaN}', "application/json"),
    Payload(b'{"truncated":', "application/json"),
    Payload(b'\xff\xfe\x00"invalid utf8"', "application/json"),
    Payload(b'{"same":"bytes"}', "application/vnd.fixture+json; charset=utf-8"),
    Payload(b'{"same":"bytes"}', "text/plain; charset=utf-8"),
    Payload(b'{ "body": "' + b"space" * 3000 + b'" }\r\n', "application/json"),
    Payload(b'{"body":"' + b"x" * 16000 + b'","key":1,"key":2}', "application/json"),
    Payload(b'{"body":"' + b"x" * 16000 + b'","number":1e+02}', "application/json"),
    Payload(b'{"body":"' + b"x" * 16000, "application/json"),
    Payload(b'{"body":"' + b"x" * 16000 + b'\xff"}', "application/json"),
])
@pytest.mark.parametrize("layout", ["flat", "shared"])
def test_exact_raw_bytes_and_media_type_survive_close_and_reopen(tmp_path, payload, layout):
    root = tmp_path / "record"
    with RecordStore.create(root, payload_layout=layout) as store:
        ref = store.put_payload(payload)
    with RecordStore.open(root) as store:
        assert store.read_payload(ref) == payload
        children = list(store.iter_payload_artifacts(ref))
        assert children
        for child in children:
            data = store.read_bytes(child)
            assert len(data) == child.size
            assert hashlib.sha256(data).hexdigest() == child.sha256


def test_growing_rewritten_and_reordered_contexts_are_exact(tmp_path):
    messages = [{"role": "system", "content": "stable " * 1200}]
    original = [*messages, {"role": "user", "content": "original " * 1200}]
    grown = [*original, {"role": "assistant", "content": "appended " * 1200}]
    edited = [*messages, {"role": "user", "content": "edited " * 1200}, grown[-1]]
    values = [original, grown, edited, list(reversed(edited)), edited[:1]]
    payloads = [Payload.json({"messages": value, "counter": index})
                for index, value in enumerate(values)]
    with RecordStore.create(tmp_path / "record", payload_layout="shared") as store:
        refs = [store.put_payload(payload) for payload in payloads]
        for payload, ref, value in zip(payloads, refs, values, strict=True):
            assert store.read_payload(ref) == payload
            assert store.read_json(ref)["messages"] == value


def test_identical_bytes_with_distinct_media_types_stay_distinct(tmp_path):
    data = Payload.json({"messages": [{"content": "same bytes " * 2048}]}).data
    payloads = [Payload(data, media) for media in (
        "application/json", "application/json; charset=utf-8", "text/plain",
    )]
    with RecordStore.create(tmp_path / "record") as store:
        refs = [store.put_payload(payload) for payload in payloads]
        assert [store.read_payload(ref) for ref in refs] == payloads
        assert refs[1].sha256 == refs[2].sha256
        assert refs[1].media_type != refs[2].media_type


def test_duplicate_byte_put_counts_actual_retention_separately_from_io(tmp_path):
    data = b"shared repeated body\n" * 1000
    with RecordStore.create(tmp_path / "record") as store:
        with measure() as first:
            ref = store.put_bytes(data)
        with measure() as duplicate:
            assert store.put_bytes(data) == ref
    totals = benchmark.tree_totals(tmp_path / "record" / "artifacts")
    assert totals["file_count"] == 1
    assert totals["apparent_bytes"] == len(data)
    assert first.counters["written_bytes"] == len(data)
    assert duplicate.counters["written_bytes"] == 0
    assert duplicate.temporary_peak_bytes == 0
    assert duplicate.counters["verified_bytes"] == len(data)


def test_synthetic_corpus_grows_and_includes_end_context():
    smaller = benchmark.synthetic_payloads(2)
    bigger = benchmark.synthetic_payloads(3)
    assert smaller == benchmark.synthetic_payloads(2)
    assert sum(len(payload.data) for _, payload in bigger) > sum(
        len(payload.data) for _, payload in smaller
    )
    end = json.loads(bigger[-1][1].data)
    assert bigger[-1][0] == "runend.details"
    assert len(end["final_control_context"]["messages"]) == 8
    assert {role for role, _ in bigger} >= {
        "node.context", "execution.context", "execution.request", "event.payload", "runend.details",
    }


def test_captured_fixture_keeps_catalog_hashes_and_nonpayload_artifacts(tmp_path):
    source = tmp_path / "source"
    context = Payload.json({"messages": [{"role": "user", "content": "body " * 3000}]})
    with RecordStore.create(source, payload_layout="flat") as store:
        context_ref = store.put_payload(context)
        state_ref = store.put_bytes(b"opaque archive", media_type="application/x-tar")
    bodies = [
        ("node", "node-1", {"context": context_ref.model_dump(mode="json")}),
        ("state", "state-1", {"manifest": state_ref.model_dump(mode="json")}),
    ]
    with sqlite3.connect(source / "catalog.sqlite") as db:
        for kind, identifier, value in bodies:
            body = json_bytes(value)
            db.execute("INSERT INTO records VALUES (?,?,?,?,?,?)", (
                kind, identifier, None, None, body.decode(), hashlib.sha256(body).hexdigest(),
            ))
    before = (source / "catalog.sqlite").read_bytes()
    capture = benchmark.load_capture(source)
    assert (source / "catalog.sqlite").read_bytes() == before
    assert capture["payloads"] == [("node.context", context)]
    result = benchmark.run_sample(tmp_path / "target", "shared", capture["payloads"], capture)
    assert result["unreferenced_artifacts"]["file_count"] == 0
    assert result["temporary_files_after_save"]["file_count"] == 0
    assert result["record_root"]["apparent_bytes"] == (
        result["retained_artifacts"]["apparent_bytes"] + result["catalog"]["apparent_bytes"]
    )
    assert benchmark.artifact_path(tmp_path / "target", state_ref.sha256).read_bytes() == b"opaque archive"
    with sqlite3.connect(tmp_path / "target" / "catalog.sqlite") as db:
        rows = db.execute("SELECT body,sha256 FROM records").fetchall()
    assert all(hashlib.sha256(body.encode()).hexdigest() == digest for body, digest in rows)


def test_paired_accounting_reports_medians_and_all_trials(tmp_path):
    result = benchmark.paired_case(tmp_path / "pairs", benchmark.synthetic_payloads(2), repeats=3)
    assert len(result["trials"]) == 3
    for layout in ("flat", "shared"):
        median = result["median"][layout]
        assert median["payload_count"] == 20
        assert median["referenced_artifact_closure"] == median["retained_artifacts"]
        assert median["unreferenced_artifacts"]["file_count"] == 0
        assert median["temporary_files_after_save"]["file_count"] == 0
        assert median["save_sec"] > 0
        assert median["read_sec"] > 0
