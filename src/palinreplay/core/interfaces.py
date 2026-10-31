"""Explicit, synchronous capture contracts for Record's model/tool ports.

Adapters serialize the public request and the full result they actually expose;
they do not serialize clients, credentials, closures or hidden model reasoning.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar

from palinreplay.core.models import Outcome, State
from palinreplay.core.serialization import json_bytes

if TYPE_CHECKING:
    from palinreplay.storage.store import RecordStore

RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class Payload:
    data: bytes
    media_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("captured payload must contain bytes")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValueError("payload media_type must be nonempty")

    @classmethod
    def json(cls, value: Any) -> "Payload":
        return cls(json_bytes(value), "application/json")


@dataclass(frozen=True)
class ResultCapture:
    raw: Payload
    # Describe the observable representation, e.g. API JSON or merged output
    # bytes. An SDK value must not be labelled as unobserved HTTP wire bytes.
    source: str
    details: dict[str, Any] = field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    outcome: Outcome = "returned"

    def __post_init__(self) -> None:
        if not isinstance(self.raw, Payload):
            raise TypeError("raw result must be a Payload")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("capture source must be explicit")
        if not isinstance(self.details, dict):
            raise TypeError("capture details must be a JSON object")
        if not isinstance(self.limitations, tuple) or not all(
            isinstance(value, str) for value in self.limitations
        ):
            raise TypeError("capture limitations must be a tuple of strings")
        if self.outcome not in ("returned", "error", "unknown"):
            raise ValueError("invalid capture outcome")


@dataclass(frozen=True)
class CallAdapter(Generic[RequestT, ResultT]):
    name: str
    execute: Callable[[RequestT], ResultT]
    capture_request: Callable[[RequestT], Payload]
    capture_result: Callable[[ResultT], ResultCapture]
    capture_context: Callable[[RequestT], Payload] | None = None
    capture_exception: Callable[[BaseException], ResultCapture] | None = None
    # One wrapper entry does not prove how many hidden provider retries occurred.
    visibility: Literal["observed-attempt", "opaque-retries"] = "opaque-retries"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("adapter name must be nonempty")
        if self.visibility not in ("observed-attempt", "opaque-retries"):
            raise ValueError("invalid execution visibility")


@dataclass(frozen=True)
class CaptureBoundary:
    """One immutable capture request shared by the Agent and state backend."""

    node_id: str
    run_id: str
    attempt_id: str
    index: int
    step_id: str | None
    parent_node_id: str | None
    accepted_event_sequence: int


@dataclass(frozen=True)
class AgentSnapshot:
    """Deterministic, read-only export of actual context and adopted input position."""

    context: Payload
    input_position: Payload

    def __post_init__(self) -> None:
        if not isinstance(self.context, Payload) or not isinstance(self.input_position, Payload):
            raise TypeError("Agent snapshot requires context and input-position Payloads")


@dataclass(frozen=True)
class BoundaryAdapter:
    """Synchronous capture while Recorder-owned call entry points are closed.

    export_agent is read-only and is checked before and after state capture.
    capture_state persists all required materials in the supplied store and
    returns a State. Reusing a State requires checking actual managed content
    and attributes at this boundary, never guessing from the tool command.
    Neither callback may advance the Agent or perform model/tool calls.
    """

    export_agent: Callable[[CaptureBoundary], AgentSnapshot]
    capture_state: Callable[["RecordStore", CaptureBoundary, State | None], State]
