"""Second bounded model pass: verified source evidence and service identity."""

import json
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from palinreplay.core.schema import Spec
from palinreplay.preprocess.candidates import Candidate
from palinreplay.preprocess.chunks import TokenCounter
from palinreplay.preprocess.shell import exact_segment, exit_code_applies
from palinreplay.preprocess.timeline import Timeline, TimelineCall

EVIDENCE_PROMPT_VERSION = "palinreplay.preprocess-evidence.v3"

EVIDENCE_SYSTEM_PROMPT = """You extract verifiable evidence for exactly one candidate service from one recorded tool call.
Return exactly one JSON object and no prose or Markdown:
{"service_name": string,
 "identity": [{"kind": "endpoint"|"program"|"config"|"cwd"|"instance"|"label",
               "value": string, "quote": string,
               "field": "request.command"|"result.output_text"}],
 "events": [{"action": "start"|"probe"|"stop",
             "observed_state": "up"|"down"|"unknown",
             "action_result": "succeeded"|"failed"|"unattributed",
             "request_quote": string|null, "output_quote": string|null,
             "executable": boolean, "dependencies": [string],
             "reason": string}],
 "unknowns": [{"kind": string, "reason": string}]}

service_name must equal the supplied candidate name. Every quote must be copied
byte-for-byte from the selected field exactly as supplied, including backslashes and
shell escape sequences. Never normalize, reformat, re-quote or reconstruct shell, Python
or JSON syntax, and never quote a whole dictionary, expression or code snippet. Copy a
short fragment instead: an absolute path, a bare port number, or a stable output token.
For an action, request_quote is the smallest complete independently runnable command
fragment that performs only that action for this service; it must not include requests
for other services. Set executable true only when that fragment is complete; list
unresolved dependencies. output_quote must quote the actual result that demonstrates the
observed state. Use up only for demonstrated availability, down only for demonstrated
unavailability or a confirmed stop, and unknown otherwise. Availability is diagnostic;
the binary Replay state means "prepare this service from this node", not readiness.
action_result describes only the start/stop action, independently of availability.
For a complete standalone start/stop call, its actual returned exit code is checked by
the program: 0 succeeds, nonzero fails, even with empty output and no health probe.
For a subcommand in a compound call, set action_result to succeeded/failed only when
output_quote is an actual result attributable to that action (e.g. its start/stop
receipt). A whole-call exit code, unrelated file result, later probe, expected value,
or echo cannot establish a subcommand's result. Use unattributed otherwise. Do not add
a probe event unless a probe actually ran. Preserve action order and copy the original
startup redirections and trailing background & in request_quote. Ignore
comments, echo text and expected values; use only the recorded command and actual result.
Identity clues must identify this service instance across calls, not merely repeat a
common directory or a shared status summary. When the call shows a service-specific
listening port or a service-specific script path, return it as an endpoint or program
clue even if it appears inside a shared status-check command. Return every applicable
strong clue, so different calls for the same service share at least one. Do not use the
shared interpreter path, the shared working directory, or the shared status summary
itself as the only identity. The empty arrays are valid when the call supplies no
evidence; explain that in unknowns.
"""

EVIDENCE_USER_PREFIX = "Extract evidence only from this candidate and complete recorded call.\n\n"

STRONG_KINDS = frozenset({"endpoint", "program", "config", "instance"})
ALL_KINDS = STRONG_KINDS | {"cwd", "label"}


class RawIdentityClue(Spec):
    kind: Literal["endpoint", "program", "config", "cwd", "instance", "label"]
    value: StrictStr
    quote: StrictStr
    field: Literal["request.command", "result.output_text"]

    @field_validator("value", "quote")
    @classmethod
    def nonempty(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("identity values and quotes must be nonempty without NUL")
        return value


class RawEvidenceEvent(Spec):
    action: Literal["start", "probe", "stop"]
    observed_state: Literal["up", "down", "unknown"]
    action_result: Literal["succeeded", "failed", "unattributed"] = "unattributed"
    request_quote: StrictStr | None = None
    output_quote: StrictStr | None = None
    executable: bool
    dependencies: tuple[StrictStr, ...] = ()
    reason: StrictStr = ""

    @field_validator("request_quote", "output_quote")
    @classmethod
    def optional_quote(cls, value):
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("quotes must be nonempty without NUL when present")
        return value

    @field_validator("dependencies")
    @classmethod
    def dependency_names(cls, value):
        if any(not item.strip() or "\x00" in item for item in value):
            raise ValueError("dependencies must be nonempty without NUL")
        return value

    @model_validator(mode="after")
    def evidence_requirements(self):
        if self.executable and self.request_quote is None:
            raise ValueError("executable evidence requires a request_quote")
        if self.observed_state != "unknown" and self.output_quote is None:
            raise ValueError("up/down state requires an output_quote")
        return self


class RawEvidenceUnknown(Spec):
    kind: StrictStr
    reason: StrictStr

    @field_validator("kind", "reason")
    @classmethod
    def nonempty(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("unknown kinds and reasons must be nonempty without NUL")
        return value


class RawEvidence(Spec):
    service_name: StrictStr
    identity: tuple[RawIdentityClue, ...] = ()
    events: tuple[RawEvidenceEvent, ...] = ()
    unknowns: tuple[RawEvidenceUnknown, ...] = ()

    @field_validator("service_name")
    @classmethod
    def nonempty(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("service_name must be nonempty without NUL")
        return value


class VerifiedSpan(Spec):
    field: Literal["request.command", "result.output_text"]
    quote: StrictStr
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(ge=0)
    artifact_sha256: StrictStr


class VerifiedClue(Spec):
    kind: Literal["endpoint", "program", "config", "cwd", "instance", "label"]
    value: StrictStr
    span: VerifiedSpan


class VerifiedEvent(Spec):
    node_index: StrictInt = Field(ge=0)
    tool_ordinal: StrictInt = Field(ge=1)
    execution_id: StrictStr
    action: Literal["start", "probe", "stop"]
    observed_state: Literal["up", "down", "unknown"]
    action_result: Literal["succeeded", "failed", "unattributed"] = "unattributed"
    result_basis: Literal["call-exit", "action-output"] | None = None
    exit_code: StrictInt | None = None
    request_span: VerifiedSpan | None = None
    output_span: VerifiedSpan | None = None
    executable: bool
    dependencies: tuple[StrictStr, ...] = ()
    reason: StrictStr = ""


class EvidenceUnknown(Spec):
    kind: StrictStr
    reason: StrictStr
    node_index: StrictInt | None = None
    execution_id: StrictStr | None = None


class CandidateEvidence(Spec):
    candidate: Candidate
    identity: tuple[VerifiedClue, ...]
    events: tuple[VerifiedEvent, ...]
    unknowns: tuple[EvidenceUnknown, ...]
    skipped: bool = False


class EvidenceDiagnostic(Spec):
    candidate_name: StrictStr
    node_index: StrictInt = Field(ge=0)
    execution_id: StrictStr
    estimated_request_tokens: StrictInt = Field(ge=0)
    duration_sec: float
    prompt_tokens: StrictInt | None = None
    completion_tokens: StrictInt | None = None
    total_tokens: StrictInt | None = None
    finish_reason: StrictStr | None = None
    skipped: bool = False


@dataclass(frozen=True)
class EvidenceTemplate:
    system: str = EVIDENCE_SYSTEM_PROMPT
    user_prefix: str = EVIDENCE_USER_PREFIX
    request_token_budget: int = 32768
    response_token_reserve: int = 4096
    message_framing_tokens: int = 4
    reply_framing_tokens: int = 3

    def messages(self, source: dict, candidate: Candidate, call: TimelineCall) -> list[dict]:
        payload = {
            "source": source,
            "candidate": candidate.model_dump(mode="json"),
            "call": call.model_dump(mode="json"),
        }
        rendered = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.user_prefix + rendered},
        ]

    def estimated_tokens(
        self, counter: TokenCounter, source: dict, candidate: Candidate, call: TimelineCall
    ) -> int:
        messages = self.messages(source, candidate, call)
        content = sum(counter.count(message["content"]) for message in messages)
        framing = len(messages) * self.message_framing_tokens + self.reply_framing_tokens
        return content + framing + self.response_token_reserve


class CompletionClient(Protocol):
    def complete(self, messages: Sequence[dict], **parameters: Any) -> Any: ...


@dataclass(frozen=True)
class ServiceEvidence:
    name: str
    aliases: tuple[str, ...]
    identity_keys: tuple[str, ...]
    items: tuple[CandidateEvidence, ...]
    identity_unknown: bool
    unknowns: tuple[EvidenceUnknown, ...]

    def events(self) -> tuple[VerifiedEvent, ...]:
        return tuple(event for item in self.items for event in item.events)

    def document(self) -> dict:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "identity_keys": list(self.identity_keys),
            "identity_unknown": self.identity_unknown,
            "items": [item.model_dump(mode="json") for item in self.items],
            "unknowns": [unknown.model_dump(mode="json") for unknown in self.unknowns],
        }


@dataclass(frozen=True)
class EvidenceBatch:
    items: tuple[CandidateEvidence, ...]
    services: tuple[ServiceEvidence, ...]
    diagnostics: tuple[EvidenceDiagnostic, ...]
    prompt_version: str = EVIDENCE_PROMPT_VERSION


def _find_unique(text: str, quote: str, *, unique: bool) -> int:
    first = text.find(quote)
    if first < 0:
        raise ValueError(f"quoted evidence is not present: {quote!r}")
    if unique and text.find(quote, first + 1) >= 0:
        raise ValueError(f"quoted evidence is ambiguous; include more context: {quote!r}")
    return first


def _span(
    call: TimelineCall,
    field_name: Literal["request.command", "result.output_text"],
    quote: str,
    *,
    unique: bool = True,
) -> VerifiedSpan:
    if field_name == "request.command":
        text, artifact = call.request.command, call.refs.request.sha256
    else:
        text = call.result.output_text
        if text is None:
            raise ValueError("cannot quote non-UTF-8 or missing output text")
        artifact = call.refs.output.sha256
    start = _find_unique(text, quote, unique=unique)
    return VerifiedSpan(
        field=field_name,
        quote=quote,
        start=start,
        end=start + len(quote),
        artifact_sha256=artifact,
    )


def _build_candidate_evidence(
    candidate: Candidate, call: TimelineCall, raw: RawEvidence
) -> CandidateEvidence:
    if raw.service_name != candidate.name:
        raise ValueError(
            f"evidence service_name {raw.service_name!r} does not match candidate {candidate.name!r}"
        )
    identity = tuple(
        VerifiedClue(
            kind=clue.kind,
            value=clue.value,
            span=_span(call, clue.field, clue.quote, unique=False),
        )
        for clue in raw.identity
    )
    events = []
    seen = set()
    for event in raw.events:
        if event.action not in candidate.actions:
            raise ValueError("evidence action was not proposed by the candidate")
        request_span = (
            _span(call, "request.command", event.request_quote)
            if event.request_quote is not None else None
        )
        output_span = (
            _span(call, "result.output_text", event.output_quote)
            if event.output_quote is not None else None
        )
        key = (event.action, request_span.start if request_span else None)
        if key in seen:
            raise ValueError("duplicate evidence action for the same command fragment")
        seen.add(key)
        action_result, basis, exit_code = "unattributed", None, None
        if (event.action in ("start", "stop") and request_span is not None
                and exact_segment(call.request.command, request_span.quote) is not None):
            if call.result.outcome == "returned" and exit_code_applies(
                call.request.command, request_span.quote
            ) and call.result.exit_code is not None:
                exit_code = call.result.exit_code
                action_result = "succeeded" if exit_code == 0 else "failed"
                basis = "call-exit"
            elif (call.result.outcome == "returned" and output_span is not None
                  and event.action_result != "unattributed"):
                action_result, basis = event.action_result, "action-output"
        events.append(VerifiedEvent(
            node_index=call.node_index,
            tool_ordinal=call.tool_ordinal,
            execution_id=call.execution_id,
            action=event.action,
            observed_state=event.observed_state,
            action_result=action_result,
            result_basis=basis,
            exit_code=exit_code,
            request_span=request_span,
            output_span=output_span,
            executable=event.executable,
            dependencies=event.dependencies,
            reason=event.reason,
        ))
    unknowns = [
        EvidenceUnknown(
            kind=unknown.kind, reason=unknown.reason, node_index=call.node_index,
            execution_id=call.execution_id,
        )
        for unknown in raw.unknowns
    ]
    if not identity:
        unknowns.append(
            EvidenceUnknown(
                kind="identity",
                reason="the call did not yield a strong service identity clue",
                node_index=call.node_index,
                execution_id=call.execution_id,
            )
        )
    if not events:
        unknowns.append(
            EvidenceUnknown(
                kind="evidence",
                reason="the call did not yield a verified service event",
                node_index=call.node_index,
                execution_id=call.execution_id,
            )
        )
    return CandidateEvidence(
        candidate=candidate,
        identity=identity,
        events=tuple(events),
        unknowns=tuple(unknowns),
    )


def _usage_value(response, name):
    usage = getattr(response, "usage", None)
    value = getattr(usage, name, None) if usage is not None else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _response_text(response) -> tuple[str, str | None]:
    choices = getattr(response, "choices", None)
    if not choices:
        raise ValueError("model response has no choices")
    choice = choices[0]
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) if message is not None else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("model response has no text content")
    refusal = getattr(message, "refusal", None)
    if refusal:
        raise ValueError(f"model refused the evidence request: {refusal}")
    return content, getattr(choice, "finish_reason", None)


def _analyze_one(
    candidate: Candidate,
    call: TimelineCall,
    source: dict,
    template: EvidenceTemplate,
    client: CompletionClient,
) -> tuple[CandidateEvidence, EvidenceDiagnostic]:
    started = time.monotonic()
    messages = template.messages(source, candidate, call)
    response = client.complete(messages, response_format={"type": "json_object"})
    text, finish_reason = _response_text(response)
    if finish_reason != "stop":
        raise ValueError(f"model response did not finish normally: {finish_reason!r}")
    try:
        raw = RawEvidence.model_validate_json(text)
    except ValueError as error:
        raise ValueError(f"model response is not strict evidence JSON: {error}") from error
    item = _build_candidate_evidence(candidate, call, raw)
    diagnostic = EvidenceDiagnostic(
        candidate_name=candidate.name,
        node_index=candidate.node_index,
        execution_id=candidate.execution_id,
        estimated_request_tokens=0,
        duration_sec=time.monotonic() - started,
        prompt_tokens=_usage_value(response, "prompt_tokens"),
        completion_tokens=_usage_value(response, "completion_tokens"),
        total_tokens=_usage_value(response, "total_tokens"),
        finish_reason=finish_reason,
    )
    return item, diagnostic


def _skipped_evidence(candidate: Candidate, reason: str) -> CandidateEvidence:
    return CandidateEvidence(
        candidate=candidate,
        identity=(),
        events=(),
        unknowns=(
            EvidenceUnknown(
                kind="evidence-budget",
                reason=reason,
                node_index=candidate.node_index,
                execution_id=candidate.execution_id,
            ),
        ),
        skipped=True,
    )


def _strong_keys(item: CandidateEvidence) -> set[str]:
    return {
        f"{clue.kind}:{clue.value}"
        for clue in item.identity
        if clue.kind in STRONG_KINDS
    }


def _ambiguous_keys(items: tuple[CandidateEvidence, ...]) -> set[str]:
    """Strong keys shared by different candidates of one execution are ambiguous."""
    by_execution: dict[tuple[int, str], list[set[str]]] = {}
    for item in items:
        key = (item.candidate.node_index, item.candidate.execution_id)
        by_execution.setdefault(key, []).append(_strong_keys(item))
    ambiguous = set()
    for keysets in by_execution.values():
        if len(keysets) < 2:
            continue
        shared = set.intersection(*keysets)
        ambiguous.update(shared)
    return ambiguous


def _find(parent: list[int], value: int) -> int:
    while parent[value] != value:
        parent[value] = parent[parent[value]]
        value = parent[value]
    return value


def _union(parent: list[int], left: int, right: int) -> None:
    left, right = _find(parent, left), _find(parent, right)
    if left != right:
        parent[max(left, right)] = min(left, right)


def merge_services(items: tuple[CandidateEvidence, ...]) -> tuple[ServiceEvidence, ...]:
    """Merge only by unambiguous strong identity evidence, never by name alone."""
    if not items:
        return ()
    ambiguous = _ambiguous_keys(items)
    keysets = []
    for item in items:
        keys = _strong_keys(item)
        if keys & ambiguous:
            keys -= ambiguous
        keysets.append(keys)
    parent = list(range(len(items)))
    owners: dict[str, list[int]] = {}
    for index, keys in enumerate(keysets):
        for key in keys:
            owners.setdefault(key, []).append(index)
    for indices in owners.values():
        for index in indices[1:]:
            _union(parent, indices[0], index)
    grouped: dict[int, list[int]] = {}
    for index in range(len(items)):
        grouped.setdefault(_find(parent, index), []).append(index)
    services = []
    for indices in sorted(grouped.values(), key=lambda group: group[0]):
        group_items = tuple(items[index] for index in indices)
        names = []
        for item in group_items:
            if item.candidate.name not in names:
                names.append(item.candidate.name)
        group_keys = sorted(set().union(*(keysets[index] for index in indices)))
        identity_unknown = not group_keys
        unknowns = [unknown for item in group_items for unknown in item.unknowns]
        if not group_keys:
            unknowns.append(
                EvidenceUnknown(
                    kind="identity",
                    reason="no strong endpoint/program/config/instance evidence links this service",
                )
            )
        services.append(
            ServiceEvidence(
                name=names[0],
                aliases=tuple(names),
                identity_keys=tuple(group_keys),
                items=group_items,
                identity_unknown=identity_unknown,
                unknowns=tuple(unknowns),
            )
        )
    return tuple(services)


def analyze_evidence(
    timeline: Timeline,
    candidates: tuple[Candidate, ...],
    counter: TokenCounter,
    template: EvidenceTemplate,
    client: CompletionClient,
    *,
    concurrency: int,
) -> EvidenceBatch:
    """Send one bounded request per candidate, then merge verified identities."""
    if not candidates:
        return EvidenceBatch(items=(), services=(), diagnostics=())
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    calls = {call.execution_id: call for call in timeline.calls}
    source = timeline.source.model_dump(mode="json")
    items: dict[int, CandidateEvidence] = {}
    diagnostics: dict[int, EvidenceDiagnostic] = {}
    pending = []
    for index, candidate in enumerate(candidates):
        call = calls.get(candidate.execution_id)
        if call is None or call.node_index != candidate.node_index:
            raise RuntimeError(
                f"candidate {candidate.name!r} references an unknown source execution "
                f"{candidate.execution_id!r}"
            )
        estimate = template.estimated_tokens(counter, source, candidate, call)
        if estimate > template.request_token_budget:
            items[index] = _skipped_evidence(
                candidate,
                f"the complete call needs {estimate} request tokens, above the "
                f"{template.request_token_budget}-token budget; it was not truncated",
            )
            diagnostics[index] = EvidenceDiagnostic(
                candidate_name=candidate.name,
                node_index=candidate.node_index,
                execution_id=candidate.execution_id,
                estimated_request_tokens=estimate,
                duration_sec=0.0,
                skipped=True,
            )
        else:
            pending.append((index, candidate, call, estimate))
    failures = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(_analyze_one, candidate, call, source, template, client): (
                index, candidate, call, estimate
            )
            for index, candidate, call, estimate in pending
        }
        for future in as_completed(futures):
            index, candidate, call, estimate = futures[future]
            try:
                item, diagnostic = future.result()
            except Exception as error:
                failures.append((candidate, error))
                for other in futures:
                    other.cancel()
            else:
                items[index] = item
                diagnostics[index] = diagnostic.model_copy(
                    update={"estimated_request_tokens": estimate}
                )
    if failures:
        candidate, error = failures[0]
        raise RuntimeError(
            f"evidence analysis failed for {candidate.name!r} at C{candidate.node_index} "
            f"({candidate.execution_id}): {error}"
        ) from error
    if set(items) != set(range(len(candidates))) or set(diagnostics) != set(range(len(candidates))):
        raise RuntimeError("evidence analysis did not return every candidate")
    ordered = tuple(items[index] for index in range(len(candidates)))
    return EvidenceBatch(
        items=ordered,
        services=merge_services(ordered),
        diagnostics=tuple(diagnostics[index] for index in range(len(candidates))),
    )
