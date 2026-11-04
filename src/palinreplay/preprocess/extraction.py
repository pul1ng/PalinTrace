"""Extract service controls with one model request per complete-call chunk."""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, StrictInt, StrictStr, field_validator

from palinreplay.core.schema import Spec
from palinreplay.preprocess.candidates import (
    Candidate,
    CompletionClient,
    _response_text,
    _usage_value,
)
from palinreplay.preprocess.chunks import CallChunk, PromptTemplate
from palinreplay.preprocess.evidence import (
    CandidateEvidence,
    RawEvidence,
    RawEvidenceEvent,
    RawIdentityClue,
    ServiceEvidence,
    _build_candidate_evidence,
)
from palinreplay.preprocess.shell import exact_segment
from palinreplay.preprocess.timeline import Timeline, TimelineCall

EXTRACTION_PROMPT_VERSION = "palinreplay.preprocess-extraction.v1"
EXTRACTION_SYSTEM_PROMPT = """Extract recorded background-service start and stop commands.
Return exactly {"events": [...]} as JSON, without prose or Markdown.
Each event has:
{"name": string, "service_key": string, "node_index": integer,
 "execution_id": string, "action": "start"|"stop", "command": string,
 "action_result": "succeeded"|"failed"|"unattributed", "output_quote": string|null}.

Read the complete recorded calls, but emit only actual service-specific start/stop
commands. Business requests, health probes, status summaries, comments and expected
values are context, not events. In particular a Python dictionary containing fields
named start/stop does not execute those actions. {"events":[]} is a complete valid
answer when there are no service controls.
Copy node_index and execution_id from the call. command must be the smallest complete
independently executable original shell fragment for that service, copied exactly,
including quotes, arguments, redirections and a trailing background &. For a start
followed by && or ||, return only the startup fragment. Include a startup even when
it failed, returned no output, or its result cannot be attributed.
service_key must be copied from command and identify the service instance across
start and stop calls. Prefer the exact service-specific lifecycle/control script
path; otherwise use a shared instance-specific config/pidfile path or endpoint.
Do not use a display name, shared interpreter, common parent directory or shared
status-check code as the key. Separate services need distinct keys. If the same
service starts in one chunk and stops in another, use the same original key.
An isolated stop is still an event; other chunks may contain its start.
name is only a display label, such as A, B or C.

action_result refers to the start/stop command, not to the service's availability.
For a whole standalone command the program will use its actual exit code.
For a fragment inside a compound call, set succeeded/failed only with output_quote
copied exactly from a recorded result attributable to that action, such as its
start/stop receipt. A whole-call exit code, a later health check, an echo or an
expected value cannot establish the fragment's result. Use unattributed and null
when there is no attributable result. Preserve every real stop receipt.
Return all start/stop events, including multiple actions within one call.
"""
EXTRACTION_USER_PREFIX = "Extract service controls from these complete recorded calls.\n\n"


class ServiceEvent(Spec):
    name: StrictStr
    service_key: StrictStr
    node_index: StrictInt = Field(ge=0)
    execution_id: StrictStr
    action: Literal["start", "stop"]
    command: StrictStr
    action_result: Literal["succeeded", "failed", "unattributed"] = "unattributed"
    output_quote: StrictStr | None = None

    @field_validator("name", "service_key", "execution_id", "command", "output_quote")
    @classmethod
    def nonempty(cls, value):
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("event fields must be nonempty without NUL")
        return value


class ExtractionResponse(Spec):
    events: tuple[ServiceEvent, ...]


@dataclass(frozen=True)
class ExtractionBatch:
    events: tuple[ServiceEvent, ...]
    services: tuple[ServiceEvidence, ...]
    diagnostics: tuple[dict, ...]


def extraction_template(**budget) -> PromptTemplate:
    return PromptTemplate(
        system=EXTRACTION_SYSTEM_PROMPT, user_prefix=EXTRACTION_USER_PREFIX, **budget,
    )


def _extract_chunk(chunk: CallChunk, template: PromptTemplate, client: CompletionClient, source):
    started = time.monotonic()
    response = client.complete(
        template.messages(source, chunk.calls), response_format={"type": "json_object"},
    )
    text, finish = _response_text(response)
    if finish != "stop":
        raise ValueError(f"model response did not finish normally: {finish!r}")
    parsed = ExtractionResponse.model_validate_json(text)
    calls = {call.execution_id: call for call in chunk.calls}
    for event in parsed.events:
        call = calls.get(event.execution_id)
        if call is None or event.node_index != call.node_index:
            raise ValueError(
                f"{event.name} C{event.node_index}/{event.execution_id}: "
                "event does not identify a call in this chunk"
            )
    diagnostic = {
        **chunk.document(),
        "duration_sec": time.monotonic() - started,
        "prompt_tokens": _usage_value(response, "prompt_tokens"),
        "completion_tokens": _usage_value(response, "completion_tokens"),
        "total_tokens": _usage_value(response, "total_tokens"),
        "finish_reason": finish,
        "event_count": len(parsed.events),
    }
    return parsed.events, diagnostic


def _verify_event(event: ServiceEvent, call: TimelineCall) -> CandidateEvidence:
    location = f"{event.name} C{event.node_index}/{event.execution_id}"
    if event.service_key not in event.command:
        raise ValueError(f"{location}: service_key must occur in its original command fragment")
    if exact_segment(call.request.command, event.command) is None:
        raise ValueError(f"{location}: command is not a complete original shell fragment")
    candidate = Candidate(
        name=event.name, node_index=event.node_index, execution_id=event.execution_id,
        actions=(event.action,), candidate_state="unknown",
    )
    raw = RawEvidence(
        service_name=event.name,
        identity=(RawIdentityClue(
            kind="instance", value=event.service_key, quote=event.service_key,
            field="request.command",
        ),),
        events=(RawEvidenceEvent(
            action=event.action, observed_state="unknown", action_result=event.action_result,
            request_quote=event.command, output_quote=event.output_quote, executable=True,
        ),),
    )
    try:
        return _build_candidate_evidence(candidate, call, raw)
    except ValueError as error:
        raise ValueError(f"{location}: {error}") from error


def _services(events: tuple[ServiceEvent, ...], calls) -> tuple[ServiceEvidence, ...]:
    groups: dict[str, list[CandidateEvidence]] = {}
    seen = set()
    for event in events:
        item = _verify_event(event, calls[event.execution_id])
        control = item.events[0]
        key = (event.execution_id, control.request_span.start)
        if key in seen:
            raise ValueError(
                f"{event.name} C{event.node_index}/{event.execution_id}: duplicate control fragment"
            )
        seen.add(key)
        groups.setdefault(event.service_key, []).append(item)
    services = []
    for service_key, items in groups.items():
        aliases = tuple(dict.fromkeys(item.candidate.name for item in items))
        services.append(ServiceEvidence(
            name=aliases[0], aliases=aliases, identity_keys=("instance:" + service_key,),
            items=tuple(items), identity_unknown=False, unknowns=(),
        ))
    return tuple(services)


def extract_services(
    timeline: Timeline, chunks: tuple[CallChunk, ...], template: PromptTemplate,
    client: CompletionClient, *, concurrency: int,
) -> ExtractionBatch:
    """Collect every block once, then validate and group controls in source order."""
    if not chunks:
        return ExtractionBatch(events=(), services=(), diagnostics=())
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    source = timeline.source.model_dump(mode="json")
    returned, diagnostics = {}, {}
    failures = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(_extract_chunk, chunk, template, client, source): chunk
            for chunk in chunks
        }
        for future in as_completed(futures):
            chunk = futures[future]
            if future.cancelled():
                continue
            try:
                events, diagnostic = future.result()
            except Exception as error:
                failures.append((chunk, error))
                for pending in futures:
                    pending.cancel()
            else:
                returned[chunk.index] = events
                diagnostics[chunk.index] = diagnostic
    if failures:
        chunk, error = failures[0]
        raise RuntimeError(
            f"service extraction failed for chunk {chunk.index} "
            f"(C{chunk.first_call.node_index}-C{chunk.last_call.node_index}): {error}"
        ) from error
    calls = {call.execution_id: call for call in timeline.calls}
    events = tuple(sorted(
        (event for index in range(len(chunks)) for event in returned[index]),
        key=lambda event: (
            event.node_index, calls[event.execution_id].tool_ordinal,
            calls[event.execution_id].request.command.find(event.command),
        ),
    ))
    return ExtractionBatch(
        events=events, services=_services(events, calls),
        diagnostics=tuple(diagnostics[index] for index in range(len(chunks))),
    )
