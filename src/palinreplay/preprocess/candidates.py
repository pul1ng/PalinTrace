"""Bounded concurrent model recognition of service-event candidates."""

import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import Field, StrictInt, StrictStr, field_validator

from palinreplay.core.schema import Spec
from palinreplay.preprocess.chunks import CallChunk, PromptTemplate
from palinreplay.preprocess.timeline import Timeline, TimelineCall

CANDIDATE_PROMPT_VERSION = "palinreplay.preprocess-candidates.v1"

CANDIDATE_SYSTEM_PROMPT = """You analyze a recorded coding-agent trajectory and identify background services.
Return exactly one JSON object with one key, "candidates", and no prose or Markdown.
Each candidate describes one service in one recorded tool call:
{"name": string, "node_index": integer, "execution_id": string,
 "actions": array of "start"|"probe"|"stop", "candidate_state": "up"|"down"|"unknown"}.
Only use node_index and execution_id values copied from the supplied calls. At most one
candidate per service per call. List actions in the order observed within the call. A start
attempt alone means "unknown": a zero exit status does not prove the service became ready.
Use "up" only when the actual result demonstrates availability, and "down" only when it
demonstrates unavailability or a completed stop. Ignore comments, echo text and expectations
inside commands; judge from the actual recorded command and result. Do not infer services that
the calls do not support. {"candidates":[]} is a valid complete answer when no service is shown.
"""

CANDIDATE_USER_PREFIX = (
    "Analyze these complete recorded tool calls. Return only the JSON object.\n\n"
)


class Candidate(Spec):
    name: StrictStr
    node_index: StrictInt = Field(ge=0)
    execution_id: StrictStr
    actions: tuple[Literal["start", "probe", "stop"], ...]
    candidate_state: Literal["up", "down", "unknown"]

    @field_validator("name", "execution_id")
    @classmethod
    def nonempty(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("candidate name and execution_id must be nonempty without NUL")
        return value


class CandidateResponse(Spec):
    candidates: tuple[Candidate, ...]


class CompletionClient(Protocol):
    def complete(self, messages: Sequence[dict], **parameters: Any) -> Any: ...


class ChunkDiagnostic(Spec):
    chunk_index: StrictInt = Field(ge=0)
    first_node: StrictInt = Field(ge=0)
    last_node: StrictInt = Field(ge=0)
    call_count: StrictInt = Field(ge=1)
    estimated_request_tokens: StrictInt = Field(ge=0)
    duration_sec: float
    prompt_tokens: StrictInt | None = None
    completion_tokens: StrictInt | None = None
    total_tokens: StrictInt | None = None
    finish_reason: StrictStr | None = None
    response_chars: StrictInt = Field(ge=0)
    candidate_count: StrictInt = Field(ge=0)


@dataclass(frozen=True)
class CandidateBatch:
    candidates: tuple[Candidate, ...]
    diagnostics: tuple[ChunkDiagnostic, ...]
    prompt_version: str = CANDIDATE_PROMPT_VERSION


def candidate_template(**budget) -> PromptTemplate:
    """One fixed template shared by every independently analyzed chunk."""
    return PromptTemplate(
        system=CANDIDATE_SYSTEM_PROMPT,
        user_prefix=CANDIDATE_USER_PREFIX,
        **budget,
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
        raise ValueError(f"model refused the analysis: {refusal}")
    return content, getattr(choice, "finish_reason", None)


def _validate_candidates(
    response: CandidateResponse, chunk: CallChunk
) -> tuple[Candidate, ...]:
    by_execution = {call.execution_id: call for call in chunk.calls}
    seen = set()
    for candidate in response.candidates:
        call = by_execution.get(candidate.execution_id)
        if call is None:
            raise ValueError(
                f"candidate for {candidate.name!r} references execution "
                f"{candidate.execution_id!r} outside this chunk"
            )
        if candidate.node_index != call.node_index:
            raise ValueError(
                f"candidate for {candidate.name!r} has node {candidate.node_index} "
                f"but execution {candidate.execution_id!r} is at C{call.node_index}"
            )
        key = (candidate.name, candidate.execution_id)
        if key in seen:
            raise ValueError(
                f"duplicate candidate for {candidate.name!r} in execution "
                f"{candidate.execution_id!r}"
            )
        seen.add(key)
    return response.candidates


def _analyze_chunk(
    chunk: CallChunk, template: PromptTemplate, client: CompletionClient, source: dict
) -> tuple[int, tuple[Candidate, ...], ChunkDiagnostic]:
    started = time.monotonic()
    messages = template.messages(source, chunk.calls)
    response = client.complete(messages, response_format={"type": "json_object"})
    text, finish_reason = _response_text(response)
    if finish_reason != "stop":
        raise ValueError(f"model response did not finish normally: {finish_reason!r}")
    try:
        parsed = CandidateResponse.model_validate_json(text)
    except ValueError as error:
        raise ValueError(f"model response is not the strict candidate JSON: {error}") from error
    candidates = _validate_candidates(parsed, chunk)
    diagnostic = ChunkDiagnostic(
        chunk_index=chunk.index,
        first_node=chunk.first_call.node_index,
        last_node=chunk.last_call.node_index,
        call_count=len(chunk.calls),
        estimated_request_tokens=chunk.estimated_tokens,
        duration_sec=time.monotonic() - started,
        prompt_tokens=_usage_value(response, "prompt_tokens"),
        completion_tokens=_usage_value(response, "completion_tokens"),
        total_tokens=_usage_value(response, "total_tokens"),
        finish_reason=finish_reason,
        response_chars=len(text),
        candidate_count=len(candidates),
    )
    return chunk.index, candidates, diagnostic


def _order_key(candidate: Candidate, calls: dict[str, TimelineCall]) -> tuple:
    call = calls[candidate.execution_id]
    return (
        call.node_index,
        call.tool_ordinal,
        candidate.name,
        candidate.candidate_state,
        candidate.actions,
    )


def recognize_candidates(
    timeline: Timeline,
    chunks: tuple[CallChunk, ...],
    template: PromptTemplate,
    client: CompletionClient,
    *,
    concurrency: int,
) -> CandidateBatch:
    """Send every chunk independently, then merge by original source order."""
    if not chunks:
        return CandidateBatch(candidates=(), diagnostics=())
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    source = timeline.source.model_dump(mode="json")
    by_index: dict[int, tuple[Candidate, ...]] = {}
    diagnostics: dict[int, ChunkDiagnostic] = {}
    failures = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(_analyze_chunk, chunk, template, client, source): chunk
            for chunk in chunks
        }
        for future in as_completed(futures):
            chunk = futures[future]
            try:
                index, candidates, diagnostic = future.result()
            except BaseException as error:
                failures.append((chunk, error))
                for pending in futures:
                    pending.cancel()
            else:
                by_index[index] = candidates
                diagnostics[index] = diagnostic
    if failures:
        chunk, error = failures[0]
        detail = f"chunk {chunk.index} (C{chunk.first_call.node_index}-C{chunk.last_call.node_index})"
        raise RuntimeError(f"candidate analysis failed for {detail}: {error}") from error
    if set(by_index) != set(range(len(chunks))):
        raise RuntimeError("candidate analysis did not return every chunk")
    calls = {call.execution_id: call for call in timeline.calls}
    ordered = tuple(
        sorted(
            (candidate for index in range(len(chunks)) for candidate in by_index[index]),
            key=lambda candidate: _order_key(candidate, calls),
        )
    )
    return CandidateBatch(
        candidates=ordered,
        diagnostics=tuple(diagnostics[index] for index in range(len(chunks))),
    )
