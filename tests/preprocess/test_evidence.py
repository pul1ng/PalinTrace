import json

import pytest

from palinreplay.core.models import ArtifactRef
from palinreplay.preprocess.candidates import Candidate
from palinreplay.preprocess.evidence import (
    EVIDENCE_PROMPT_VERSION,
    CandidateEvidence,
    EvidenceTemplate,
    RawEvidence,
    VerifiedClue,
    VerifiedSpan,
    analyze_evidence,
    merge_services,
)
from palinreplay.preprocess.timeline import (
    Timeline,
    TimelineCall,
    TimelineRequest,
    TimelineResult,
    TimelineSource,
)


def make_call(node=19, execution="ff86", command="", output="", output_text=True):
    return TimelineCall(
        node_index=node,
        tool_ordinal=1,
        execution_id=execution,
        tool_name="mini.bash",
        request=TimelineRequest(
            command=command, interpreter=("bash", "-c"), cwd="/testbed", env={}, timeout_sec=30
        ),
        result=TimelineResult(
            outcome="returned" if output_text else "unknown",
            exit_code=0,
            output_text=output if output_text else None,
        ),
        refs={
            "request": ArtifactRef(sha256="1" * 64, size=1),
            "output": ArtifactRef(sha256="2" * 64, size=1),
            "details": ArtifactRef(sha256="3" * 64, size=1),
        },
    )


def make_timeline(*calls):
    return Timeline(
        source=TimelineSource(
            run_id="run", start_index=0, end_index=40,
            digest_scheme="sha256-canonical-selection-json-v1", selection_sha256="0" * 64,
        ),
        calls=calls,
    )


def candidate(name="B", node=19, execution="ff86", actions=("start", "probe"), state="up"):
    return Candidate(
        name=name, node_index=node, execution_id=execution,
        actions=actions, candidate_state=state,
    )


def raw(**overrides):
    values = {
        "service_name": "B",
        "identity": [
            {"kind": "program", "value": "/srv/B/lifecycle.py",
             "quote": "/srv/B/lifecycle.py", "field": "request.command"}
        ],
        "events": [
            {"action": "start", "observed_state": "up",
             "request_quote": "/srv/B/lifecycle.py start",
             "output_quote": "B_READY", "executable": True,
             "dependencies": [], "reason": "started and ready"},
            {"action": "probe", "observed_state": "up",
             "request_quote": "/srv/B/request.py",
             "output_quote": "B_OK", "executable": True,
             "dependencies": [], "reason": "request succeeded"},
        ],
        "unknowns": [],
    }
    values.update(overrides)
    return json.dumps(values)


class FakeClient:
    def __init__(self, answers):
        self.answers = answers
        self.requests = []

    def complete(self, messages, **parameters):
        self.requests.append((messages, parameters))
        payload = json.loads(messages[1]["content"].split("\n\n", 1)[1])
        key = (payload["candidate"]["name"], payload["candidate"]["execution_id"])
        value = self.answers[key]
        if isinstance(value, BaseException):
            raise value
        return type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {
                            "message": type(
                                "Message", (), {"content": value, "refusal": None}
                            )(),
                            "finish_reason": "stop",
                        },
                    )()
                ],
                "usage": type(
                    "Usage", (), {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
                )(),
            },
        )()


class CharCounter:
    name = "char"

    def count(self, text):
        return len(text)


def test_verified_evidence_binds_quotes_to_source_spans_and_hashes():
    call = make_call(
        command="/srv/B/lifecycle.py start && /srv/A/request.py && /srv/B/request.py",
        output="B_READY\nA_OK\nB_OK\n",
    )
    batch = analyze_evidence(
        make_timeline(call),
        (candidate(),),
        CharCounter(),
        EvidenceTemplate(request_token_budget=100000),
        FakeClient({("B", "ff86"): raw()}),
        concurrency=1,
    )
    item = batch.items[0]
    assert item.candidate.name == "B" and not item.skipped
    assert [event.action for event in item.events] == ["start", "probe"]
    start, probe = item.events
    assert start.request_span.start == call.request.command.index("/srv/B/lifecycle.py start")
    assert start.request_span.artifact_sha256 == "1" * 64
    assert start.output_span.start == 0 and start.output_span.end == len("B_READY")
    assert probe.output_span.artifact_sha256 == "2" * 64
    clue = item.identity[0]
    assert clue.value == "/srv/B/lifecycle.py"
    assert call.request.command[clue.span.start:clue.span.end] == clue.span.quote
    assert batch.services[0].name == "B"
    assert batch.services[0].identity_keys == ("program:/srv/B/lifecycle.py",)
    assert batch.prompt_version == EVIDENCE_PROMPT_VERSION


def test_unknown_or_ambiguous_quote_fails_analysis():
    call = make_call(command="/srv/B/lifecycle.py start /srv/B/request.py", output="B_OK\nB_OK\n")
    for value in (
        raw(identity=[
            {"kind": "program", "value": "x", "quote": "missing", "field": "request.command"}
        ]),
        raw(events=[
            {"action": "probe", "observed_state": "up", "request_quote": "/srv/B/request.py",
             "output_quote": "B_OK", "executable": True, "dependencies": [], "reason": "ok"}
        ]),
    ):
        with pytest.raises(RuntimeError, match="evidence analysis failed"):
            analyze_evidence(
                make_timeline(call), (candidate(),), CharCounter(),
                EvidenceTemplate(request_token_budget=100000),
                FakeClient({("B", "ff86"): value}), concurrency=1,
            )


def test_state_without_result_quote_and_non_utf8_output_are_rejected():
    call = make_call(command="/srv/B/lifecycle.py start", output="", output_text=False)
    with pytest.raises(RuntimeError, match="output"):
        analyze_evidence(
            make_timeline(call), (candidate(),), CharCounter(),
            EvidenceTemplate(request_token_budget=100000),
            FakeClient({("B", "ff86"): raw()}), concurrency=1,
        )


def test_budget_skip_is_an_explicit_unknown_without_a_model_call():
    call = make_call(command="/srv/B/lifecycle.py start", output="B_READY\n")
    client = FakeClient({("B", "ff86"): raw()})
    batch = analyze_evidence(
        make_timeline(call), (candidate(),), CharCounter(),
        EvidenceTemplate(request_token_budget=1), client, concurrency=1,
    )
    assert client.requests == []
    assert batch.items[0].skipped
    assert batch.items[0].unknowns[0].kind == "evidence-budget"
    assert batch.diagnostics[0].skipped
    assert batch.services[0].identity_unknown


def test_model_failure_and_non_strict_json_fail():
    call = make_call(command="/srv/B/lifecycle.py start", output="B_READY\n")
    for value in (RuntimeError("boom"), '{"service_name":"B","extra":1}'):
        with pytest.raises(RuntimeError, match="evidence analysis failed"):
            analyze_evidence(
                make_timeline(call), (candidate(),), CharCounter(),
                EvidenceTemplate(request_token_budget=100000),
                FakeClient({("B", "ff86"): value}), concurrency=1,
            )


def evidence_item(name, execution, node, program=None):
    clues = ()
    if program:
        clues = (
            VerifiedClue(
                kind="program", value=program,
                span=VerifiedSpan(
                    field="request.command", quote=program, start=0, end=len(program),
                    artifact_sha256="1" * 64,
                ),
            ),
        )
    return CandidateEvidence(
        candidate=candidate(name=name, node=node, execution=execution),
        identity=clues,
        events=(),
        unknowns=(),
    )


def test_identity_merge_uses_strong_keys_not_names():
    same_program = merge_services(
        (evidence_item("A", "e1", 5, "/srv/A/lifecycle.py"),
         evidence_item("A", "e2", 25, "/srv/A/lifecycle.py"))
    )
    assert len(same_program) == 1 and same_program[0].name == "A"

    different_program = merge_services(
        (evidence_item("A", "e1", 5, "/srv/A/lifecycle.py"),
         evidence_item("A", "e2", 25, "/srv/B/lifecycle.py"))
    )
    assert len(different_program) == 2

    aliases = merge_services(
        (evidence_item("A", "e1", 5, "/srv/A/lifecycle.py"),
         evidence_item("alias", "e2", 25, "/srv/A/lifecycle.py"))
    )
    assert len(aliases) == 1
    assert aliases[0].name == "A" and aliases[0].aliases == ("A", "alias")


def test_ambiguous_same_call_identity_and_missing_identity_are_unknown():
    ambiguous = merge_services(
        (evidence_item("A", "e1", 19, "/srv/shared"), evidence_item("B", "e1", 19, "/srv/shared"))
    )
    assert len(ambiguous) == 2
    assert all(service.identity_unknown for service in ambiguous)
    assert all(any(u.kind == "identity" for u in s.unknowns) for s in ambiguous)

    unknown = merge_services((evidence_item("A", "e1", 5),))
    assert len(unknown) == 1 and unknown[0].identity_unknown
    assert unknown[0].unknowns and unknown[0].unknowns[-1].kind == "identity"


def test_raw_evidence_requires_state_quotes_and_executable_request():
    with pytest.raises(ValueError, match="output_quote"):
        RawEvidence.model_validate(json.loads(raw(events=[
            {"action": "probe", "observed_state": "up", "request_quote": "/srv/B/request.py",
             "output_quote": None, "executable": True, "dependencies": [], "reason": "ok"}
        ])))
    with pytest.raises(ValueError, match="request_quote"):
        RawEvidence.model_validate(json.loads(raw(events=[
            {"action": "start", "observed_state": "unknown", "request_quote": None,
             "output_quote": None, "executable": True, "dependencies": [], "reason": "start"}
        ])))


def test_shared_weak_key_does_not_invalidate_unique_identity_keys():
    shared = merge_services((
        evidence_item("A", "e1", 19, "/srv/shared"),
        evidence_item("B", "e1", 19, "/srv/shared"),
        evidence_item("A", "e2", 20, "/srv/A"),
        evidence_item("B", "e2", 21, "/srv/B"),
    ))
    by_name = {service.name: service for service in shared}
    assert set(by_name) == {"A", "B"}
    assert all(not service.identity_unknown for service in by_name.values())
    assert by_name["A"].identity_keys == ("program:/srv/A",)
    assert by_name["B"].identity_keys == ("program:/srv/B",)


def launch_evidence(command, quote, *, exit_code=0, outcome="returned", output="",
                    action="start", action_result="unattributed", output_quote=None):
    call = make_call(node=1, execution="launch", command=command, output=output)
    call = call.model_copy(update={"result": TimelineResult(
        outcome=outcome, exit_code=exit_code, output_text=output)})
    answer = raw(service_name="A", identity=[{
        "kind": "program", "value": "./start-A.sh", "quote": "./start-A.sh",
        "field": "request.command"}], events=[{
            "action": action, "observed_state": "unknown", "action_result": action_result,
            "request_quote": quote, "output_quote": output_quote,
            "executable": True, "dependencies": [], "reason": "fixture action attribution"}])
    batch = analyze_evidence(make_timeline(call),
        (candidate("A", 1, "launch", (action,), "unknown"),), CharCounter(),
        EvidenceTemplate(request_token_budget=100000), FakeClient({("A", "launch"): answer}),
        concurrency=1)
    return batch.items[0].events[0]


@pytest.mark.parametrize("action", ["start", "stop"])
@pytest.mark.parametrize("exit_code,expected", [(0, "succeeded"), (17, "failed"), (None, "unattributed")])
def test_whole_call_exit_is_action_result_with_empty_output_and_no_readiness(action, exit_code, expected):
    command = "./start-A.sh >a.log 2>&1 &" if action == "start" else "./start-A.sh --stop"
    event = launch_evidence(command, command, action=action, exit_code=exit_code,
                            action_result="succeeded")
    assert event.action_result == expected
    assert event.exit_code == exit_code
    assert event.result_basis == ("call-exit" if exit_code is not None else None)
    assert event.output_span is None


@pytest.mark.parametrize("command,quote,exit_code", [
    ("./start-A.sh; true", "./start-A.sh", 0),
    ("true && ./start-A.sh", "./start-A.sh", 0),
    ("./start-A.sh && false", "./start-A.sh", 1),
    ("./start-A.sh & printf done", "./start-A.sh &", 0),
    ("./start-A.sh | true", "./start-A.sh | true", 0),
    ("! ./start-A.sh", "! ./start-A.sh", 0),
])
def test_compound_exit_cannot_be_assigned_to_an_action_without_its_own_result(command, quote, exit_code):
    event = launch_evidence(command, quote, exit_code=exit_code, action_result="succeeded")
    assert event.action_result == "unattributed"
    assert event.exit_code is None and event.result_basis is None


def test_compound_action_result_uses_quoted_receipt_instead_of_overall_failure():
    event = launch_evidence("./start-A.sh && false", "./start-A.sh", exit_code=1,
                            output="A_STARTED\n", output_quote="A_STARTED", action_result="succeeded")
    assert event.action_result == "succeeded"
    assert event.result_basis == "action-output" and event.exit_code is None


@pytest.mark.parametrize("outcome", ["unknown", "error"])
def test_uncertain_tool_result_does_not_adopt_its_exit(outcome):
    event = launch_evidence("./start-A.sh", "./start-A.sh", outcome=outcome)
    assert event.action_result == "unattributed"
