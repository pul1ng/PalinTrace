import json
import threading
import time
from types import SimpleNamespace

import pytest

from palinreplay.core.models import ArtifactRef
from palinreplay.preprocess.candidates import (
    CANDIDATE_PROMPT_VERSION,
    CANDIDATE_SYSTEM_PROMPT,
    Candidate,
    CandidateBatch,
    candidate_template,
    recognize_candidates,
)
from palinreplay.preprocess.chunks import CallChunk
from palinreplay.preprocess.timeline import (
    Timeline,
    TimelineCall,
    TimelineRequest,
    TimelineResult,
    TimelineSource,
)


def call(node_index, ordinal, execution_id):
    return TimelineCall(
        node_index=node_index,
        tool_ordinal=ordinal,
        execution_id=execution_id,
        tool_name="mini.bash",
        request=TimelineRequest(
            command="start service", interpreter=("bash", "-c"), cwd="/testbed",
            env={}, timeout_sec=30,
        ),
        result=TimelineResult(outcome="returned", exit_code=0, output_text="ok\n"),
        refs={
            "request": ArtifactRef(sha256="1" * 64, size=1),
            "output": ArtifactRef(sha256="2" * 64, size=1),
            "details": ArtifactRef(sha256="3" * 64, size=1),
        },
    )


def timeline(calls):
    return Timeline(
        source=TimelineSource(
            run_id="run", start_index=0, end_index=4,
            digest_scheme="sha256-canonical-selection-json-v1", selection_sha256="0" * 64,
        ),
        calls=tuple(calls),
    )


def chunk(index, calls):
    return CallChunk(index=index, calls=tuple(calls), estimated_tokens=100)


def model_response(candidates, finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=json.dumps({"candidates": candidates}), refusal=None),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


class FakeClient:
    def __init__(self, by_execution, delay=None):
        self.by_execution = by_execution
        self.delay = delay or {}
        self.requests = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.barrier = None

    def complete(self, messages, **parameters):
        payload = json.loads(messages[1]["content"].split("\n\n", 1)[1])
        execution_id = payload["calls"][0]["execution_id"]
        with self.lock:
            self.requests.append((messages, parameters))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=2)
            time.sleep(self.delay.get(execution_id, 0))
            value = self.by_execution[execution_id]
            if isinstance(value, BaseException):
                raise value
            return value
        finally:
            with self.lock:
                self.active -= 1


def test_c19_like_response_can_name_two_services_and_is_stably_merged():
    call_19 = call(19, 1, "ff86")
    client = FakeClient(
        {
            "ff86": model_response(
                [
                    {"name": "B", "node_index": 19, "execution_id": "ff86",
                     "actions": ["start", "probe"], "candidate_state": "up"},
                    {"name": "A", "node_index": 19, "execution_id": "ff86",
                     "actions": ["probe"], "candidate_state": "up"},
                ]
            )
        }
    )
    batch = recognize_candidates(
        timeline([call_19]), (chunk(0, [call_19]),), candidate_template(), client, concurrency=1
    )
    assert [item.name for item in batch.candidates] == ["A", "B"]
    assert batch.candidates[1].actions == ("start", "probe")
    assert batch.prompt_version == CANDIDATE_PROMPT_VERSION
    assert len(batch.diagnostics) == 1
    assert batch.diagnostics[0].candidate_count == 2
    messages, parameters = client.requests[0]
    assert messages[0] == {"role": "system", "content": CANDIDATE_SYSTEM_PROMPT}
    assert parameters == {"response_format": {"type": "json_object"}}


def test_empty_candidates_is_success_but_invalid_shapes_fail():
    call_19 = call(19, 1, "ff86")
    batch = recognize_candidates(
        timeline([call_19]),
        (chunk(0, [call_19]),),
        candidate_template(),
        FakeClient({"ff86": model_response([])}),
        concurrency=1,
    )
    assert batch == CandidateBatch(
        candidates=(), diagnostics=batch.diagnostics, prompt_version=CANDIDATE_PROMPT_VERSION
    )

    invalid = [
        [{"name": "A", "node_index": 19, "execution_id": "ff86", "actions": [],
          "candidate_state": "up", "extra": True}],
        [{"name": "A", "node_index": 18, "execution_id": "ff86", "actions": [],
          "candidate_state": "up"}],
        [{"name": "A", "node_index": 19, "execution_id": "outside", "actions": [],
          "candidate_state": "up"}],
        [{"name": "A", "node_index": 19, "execution_id": "ff86", "actions": [],
          "candidate_state": "up"}] * 2,
        [{"name": "A", "node_index": 19, "execution_id": "ff86", "actions": ["restart"],
          "candidate_state": "up"}],
    ]
    for candidates in invalid:
        with pytest.raises(RuntimeError, match="candidate analysis failed"):
            recognize_candidates(
                timeline([call_19]),
                (chunk(0, [call_19]),),
                candidate_template(),
                FakeClient({"ff86": model_response(candidates)}),
                concurrency=1,
            )


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"candidates":[]}', refusal=None),
                finish_reason="length",
            )]
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=None, refusal=None), finish_reason="stop"
            )]
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"candidates":[]}', refusal="no"),
                finish_reason="stop",
            )]
        ),
        SimpleNamespace(choices=[]),
    ],
)
def test_truncated_empty_refused_or_missing_response_is_a_failure(response):
    call_19 = call(19, 1, "ff86")
    with pytest.raises(RuntimeError, match="candidate analysis failed"):
        recognize_candidates(
            timeline([call_19]),
            (chunk(0, [call_19]),),
            candidate_template(),
            FakeClient({"ff86": response}),
            concurrency=1,
        )


def test_chunks_run_concurrently_with_a_bound():
    first, second = call(1, 1, "first"), call(2, 1, "second")
    client = FakeClient(
        {
            "first": model_response([]),
            "second": model_response([]),
        }
    )
    client.barrier = threading.Barrier(2)
    batch = recognize_candidates(
        timeline([first, second]),
        (chunk(0, [first]), chunk(1, [second])),
        candidate_template(),
        client,
        concurrency=2,
    )
    assert client.max_active == 2
    assert len(batch.diagnostics) == 2


def test_results_are_ordered_by_source_not_completion_order():
    first, second = call(1, 1, "first"), call(2, 1, "second")
    client = FakeClient(
        {
            "first": model_response([
                {"name": "B", "node_index": 1, "execution_id": "first", "actions": ["start"],
                 "candidate_state": "unknown"}
            ]),
            "second": model_response([
                {"name": "A", "node_index": 2, "execution_id": "second", "actions": ["probe"],
                 "candidate_state": "up"}
            ]),
        },
        delay={"first": 0.05},
    )
    batch = recognize_candidates(
        timeline([first, second]),
        (chunk(0, [first]), chunk(1, [second])),
        candidate_template(),
        client,
        concurrency=2,
    )
    assert [(item.name, item.node_index) for item in batch.candidates] == [("B", 1), ("A", 2)]


def test_client_exception_is_reported_not_treated_as_empty_candidates():
    call_19 = call(19, 1, "ff86")
    with pytest.raises(RuntimeError, match=r"chunk 0.*boom"):
        recognize_candidates(
            timeline([call_19]),
            (chunk(0, [call_19]),),
            candidate_template(),
            FakeClient({"ff86": RuntimeError("boom")}),
            concurrency=1,
        )


def test_candidate_model_is_strict_about_fields_and_actions():
    with pytest.raises(ValueError):
        Candidate.model_validate(
            {"name": "A", "node_index": 1, "execution_id": "e", "actions": [],
             "candidate_state": "up", "extra": 1}
        )
    with pytest.raises(ValueError):
        Candidate.model_validate(
            {"name": "A", "node_index": 1, "execution_id": "e", "actions": ["bad"],
             "candidate_state": "up"}
        )


def test_evidence_filter_keeps_boundaries_and_bounds_repeated_probes():
    from palinreplay.preprocess.candidates import evidence_candidates

    def item(node, name, actions, state):
        return Candidate(
            name=name, node_index=node, execution_id=f"e{node}",
            actions=tuple(actions), candidate_state=state,
        )

    values = (
        item(1, "A", ["start"], "unknown"),
        item(2, "A", ["probe"], "up"),
        item(3, "A", ["probe"], "up"),
        item(4, "A", ["probe"], "up"),
        item(5, "A", ["probe"], "down"),
        item(6, "A", ["probe"], "down"),
        item(7, "A", ["start"], "up"),
        item(8, "A", ["probe"], "up"),
        item(9, "A", ["stop"], "down"),
        item(10, "A", ["probe"], "down"),
        item(11, "B", ["probe"], "down"),
    )
    kept = evidence_candidates(values)
    assert [(x.node_index, x.name, x.actions) for x in kept] == [
        (1, "A", ("start",)),
        (2, "A", ("probe",)),
        (3, "A", ("probe",)),
        (5, "A", ("probe",)),
        (7, "A", ("start",)),
        (8, "A", ("probe",)),
        (9, "A", ("stop",)),
    ]
