"""Budgeted, lossless chunking of complete tool records."""

import json
from dataclasses import dataclass
from typing import Protocol

from palinreplay.core.errors import RecordUsageError
from palinreplay.preprocess.config import PreprocessConfig
from palinreplay.preprocess.timeline import Timeline, TimelineCall

MESSAGE_FRAMING_TOKENS = 4
REPLY_FRAMING_TOKENS = 3


class TokenCounter(Protocol):
    name: str

    def count(self, text: str) -> int: ...


class TiktokenCounter:
    def __init__(self, encoding):
        self._encoding = encoding
        self.name = encoding.name

    def count(self, text: str) -> int:
        return len(self._encoding.encode_ordinary(text))


@dataclass(frozen=True)
class PromptTemplate:
    system: str
    user_prefix: str
    user_suffix: str = ""
    request_token_budget: int = 32768
    response_token_reserve: int = 4096
    message_framing_tokens: int = MESSAGE_FRAMING_TOKENS
    reply_framing_tokens: int = REPLY_FRAMING_TOKENS

    def messages(self, source: dict, calls: tuple[TimelineCall, ...]) -> list[dict]:
        payload = {
            "source": source,
            "calls": [call.model_dump(mode="json") for call in calls],
        }
        rendered = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.user_prefix + rendered + self.user_suffix},
        ]

    def estimated_tokens(self, counter: TokenCounter, source: dict, calls) -> int:
        messages = self.messages(source, calls)
        content = sum(counter.count(message["content"]) for message in messages)
        framing = len(messages) * self.message_framing_tokens + self.reply_framing_tokens
        return content + framing + self.response_token_reserve


@dataclass(frozen=True)
class CallChunk:
    index: int
    calls: tuple[TimelineCall, ...]
    estimated_tokens: int

    @property
    def first_call(self) -> TimelineCall:
        return self.calls[0]

    @property
    def last_call(self) -> TimelineCall:
        return self.calls[-1]

    def document(self) -> dict:
        return {
            "chunk_index": self.index,
            "first_node": self.first_call.node_index,
            "last_node": self.last_call.node_index,
            "first_execution_id": self.first_call.execution_id,
            "last_execution_id": self.last_call.execution_id,
            "call_count": len(self.calls),
            "estimated_request_tokens": self.estimated_tokens,
        }


def make_token_counter(config: PreprocessConfig) -> TokenCounter:
    """Resolve an explicit tokenizer; never estimate by characters or bytes."""
    import tiktoken

    settings = config.preprocess
    try:
        if settings.tokenizer_encoding is not None:
            return TiktokenCounter(tiktoken.get_encoding(settings.tokenizer_encoding))
        if settings.tokenizer_model is not None:
            return TiktokenCounter(tiktoken.encoding_for_model(settings.tokenizer_model))
        return TiktokenCounter(tiktoken.encoding_for_model(config.llm.model))
    except (KeyError, ValueError) as error:
        raise RecordUsageError(
            f"cannot resolve a tokenizer for model {config.llm.model!r}; set tokenizer_model "
            "or tokenizer_encoding in the [preprocess] table"
        ) from error


def plan_chunks(
    timeline: Timeline, counter: TokenCounter, template: PromptTemplate
) -> tuple[CallChunk, ...]:
    """Greedily keep each complete call once, in order, within the request budget."""
    calls = timeline.calls
    if not calls:
        return ()
    source = timeline.source.model_dump(mode="json")
    chunks = []
    start = 0
    while start < len(calls):
        stop, tokens = start, None
        while stop < len(calls):
            estimate = template.estimated_tokens(counter, source, calls[start:stop + 1])
            if estimate > template.request_token_budget:
                break
            stop += 1
            tokens = estimate
        if tokens is None:
            call = calls[start]
            raise RecordUsageError(
                f"tool execution {call.execution_id} at C{call.node_index} "
                f"(tool {call.tool_ordinal}) exceeds the {template.request_token_budget}-token "
                "request budget even when sent alone; refusing to truncate it"
            )
        chunks.append(CallChunk(index=len(chunks), calls=calls[start:stop], estimated_tokens=tokens))
        start = stop
    assert [call for chunk in chunks for call in chunk.calls] == list(calls)
    return tuple(chunks)
