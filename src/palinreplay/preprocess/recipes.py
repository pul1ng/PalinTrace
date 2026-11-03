"""Extract independently executable launch and readiness recipes."""

import re
from dataclasses import dataclass

from pydantic import Field, StrictInt, StrictStr, field_validator

from palinreplay.core.schema import Spec
from palinreplay.preprocess.evidence import ServiceEvidence, VerifiedEvent
from palinreplay.preprocess.lifecycle import ServiceInterval, ServiceLifecycle
from palinreplay.preprocess.timeline import Timeline, TimelineCall

STRONG_KINDS = frozenset({"endpoint", "program", "config", "instance"})
VAR_PATTERN = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


class ShellUnsupported(ValueError):
    pass


@dataclass(frozen=True)
class ShellSegment:
    start: int
    end: int
    text: str


class ServiceLaunch(Spec):
    start_command: StrictStr
    readiness_command: StrictStr
    cwd: StrictStr
    env: dict[StrictStr, StrictStr]
    timeout_sec: float = Field(gt=0, allow_inf_nan=False)

    @field_validator("start_command", "readiness_command")
    @classmethod
    def nonempty(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("launch commands must be nonempty without NUL")
        return value

    @field_validator("cwd")
    @classmethod
    def absolute(cls, value):
        if not value.startswith("/") or "\x00" in value:
            raise ValueError("launch cwd must be an absolute container path")
        return value


class RecipeUnknown(Spec):
    kind: StrictStr = "launch"
    reason: StrictStr
    node_index: StrictInt | None = None
    execution_id: StrictStr | None = None


@dataclass(frozen=True)
class IntervalRecipe:
    service: str
    interval: ServiceInterval
    launch: ServiceLaunch | None
    unknowns: tuple[RecipeUnknown, ...]
    probe_event: VerifiedEvent | None = None

    def document(self) -> dict:
        return {
            "service": self.service,
            "start_inclusive": self.interval.start_inclusive,
            "end_exclusive": self.interval.end_exclusive,
            "launch": self.launch.model_dump(mode="json") if self.launch else None,
            "probe": (
                {
                    "node_index": self.probe_event.node_index,
                    "execution_id": self.probe_event.execution_id,
                }
                if self.probe_event
                else None
            ),
            "unknowns": [unknown.model_dump(mode="json") for unknown in self.unknowns],
        }


def _flush(command: str, start: int, end: int) -> ShellSegment | None:
    raw = command[start:end]
    stripped = raw.strip()
    if not stripped:
        if raw:
            raise ShellUnsupported("empty command between shell operators")
        return None
    offset = start + raw.index(stripped)
    return ShellSegment(start=offset, end=offset + len(stripped), text=stripped)


def split_top_level(command: str) -> tuple[ShellSegment, ...]:
    """Split a small supported shell subset at top-level command-list operators.

    Quotes, escapes and simple pipelines are retained. Subshells, command
    substitution, backticks and here-documents are rejected rather than guessed.
    """
    segments = []
    start = 0
    quote = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == "\\":
                escaped = True
            elif char == '"':
                quote = None
            elif char == "`" or (char == "$" and index + 1 < len(command)
                                 and command[index + 1] == "("):
                raise ShellUnsupported("command substitution is unsupported")
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if char in ("`", "(", ")"):
            raise ShellUnsupported("subshell, function or command substitution is unsupported")
        if char == "$" and index + 1 < len(command) and command[index + 1] == "(":
            raise ShellUnsupported("command substitution is unsupported")
        if command.startswith("<<", index):
            raise ShellUnsupported("here-documents are unsupported")
        if command.startswith("&&", index) or command.startswith("||", index):
            segment = _flush(command, start, index)
            if segment is not None:
                segments.append(segment)
            index += 2
            start = index
            continue
        if char in (";", "\n"):
            segment = _flush(command, start, index)
            if segment is not None:
                segments.append(segment)
            index += 1
            start = index
            continue
        index += 1
    if quote is not None:
        raise ShellUnsupported("unterminated shell quote")
    if escaped:
        raise ShellUnsupported("trailing shell escape")
    segment = _flush(command, start, len(command))
    if segment is not None:
        segments.append(segment)
    if not segments:
        raise ShellUnsupported("empty command")
    return tuple(segments)


def _exact_segment(command: str, quote: str) -> str | None:
    try:
        segments = split_top_level(command)
    except ShellUnsupported:
        return None
    selected = quote.strip()
    if any(segment.text == selected for segment in segments):
        return selected
    return None


def _referenced_env(command: str, environment: dict[str, str]) -> dict[str, str] | None:
    referenced = {}
    for match in VAR_PATTERN.finditer(command):
        name = match.group(1) or match.group(2)
        if name not in environment:
            return None
        referenced[name] = environment[name]
    remainder = VAR_PATTERN.sub("", command)
    if "$" in remainder:
        return None
    return referenced


def _mentions_other_service(
    fragment: str, service: ServiceEvidence, services: tuple[ServiceEvidence, ...]
) -> str | None:
    for other in services:
        if other is service:
            continue
        for key in other.identity_keys:
            kind, _, value = key.partition(":")
            if kind in STRONG_KINDS and len(value) >= 3 and value in fragment:
                return other.name
    return None


def _unknown(event: VerifiedEvent | None, reason: str) -> RecipeUnknown:
    return RecipeUnknown(
        reason=reason,
        node_index=event.node_index if event else None,
        execution_id=event.execution_id if event else None,
    )


def _readiness(
    service: ServiceEvidence,
    start_quote: str,
    start_call: TimelineCall,
    probe_event: VerifiedEvent,
    probe_call: TimelineCall,
    services: tuple[ServiceEvidence, ...],
    timeout_sec: float,
) -> ServiceLaunch | str:
    if probe_event.action != "probe" or probe_event.request_span is None:
        return "the interval has no independently executable readiness probe"
    if not probe_event.executable:
        return "the readiness fragment was not verified as executable"
    readiness_quote = _exact_segment(probe_call.request.command, probe_event.request_span.quote)
    if readiness_quote is None:
        return "the readiness quote is not a complete top-level command fragment"
    if probe_event.dependencies:
        return "the readiness fragment has unresolved dependencies: " + ", ".join(
            probe_event.dependencies
        )
    if start_quote == readiness_quote:
        return "the readiness command is the start command and is not safe to repeat"
    if start_call.request.cwd != probe_call.request.cwd:
        return "start and readiness calls use different working directories"
    other = _mentions_other_service(readiness_quote, service, services)
    if other is not None:
        return f"the readiness fragment references another service ({other})"
    start_env = _referenced_env(start_quote, start_call.request.env)
    readiness_env = _referenced_env(readiness_quote, probe_call.request.env)
    if start_env is None or readiness_env is None:
        return "a command references an environment variable absent from the source call"
    env = dict(start_env)
    for name, value in readiness_env.items():
        if name in env and env[name] != value:
            return f"environment variable {name} differs between start and readiness"
        env[name] = value
    try:
        launch = ServiceLaunch(
            start_command=start_quote,
            readiness_command=readiness_quote,
            cwd=start_call.request.cwd,
            env=env,
            timeout_sec=timeout_sec,
        )
    except ValueError as error:
        return f"the extracted launch is not a valid service specification: {error}"
    return launch


def _recipe(
    service: ServiceEvidence,
    interval: ServiceInterval,
    services: tuple[ServiceEvidence, ...],
    calls: dict[str, TimelineCall],
    timeout_sec: float,
) -> IntervalRecipe:
    def failed(reason, event=None, probe=None):
        return IntervalRecipe(
            service=service.name,
            interval=interval,
            launch=None,
            unknowns=(_unknown(event, reason),),
            probe_event=probe,
        )

    start_event = interval.start_event
    start_call = calls.get(start_event.execution_id)
    if start_call is None:
        return failed("the recipe references a call outside the analyzed timeline", start_event)
    if start_event.action != "start" or start_event.request_span is None:
        return failed("the confirmed interval has no verified start command", start_event)
    if not start_event.executable:
        return failed("the start fragment was not verified as independently executable", start_event)
    start_quote = _exact_segment(start_call.request.command, start_event.request_span.quote)
    if start_quote is None:
        return failed("the start quote is not a complete top-level command fragment", start_event)
    if start_event.dependencies:
        return failed(
            "the start fragment has unresolved dependencies: "
            + ", ".join(start_event.dependencies),
            start_event,
        )
    other = _mentions_other_service(start_quote, service, services)
    if other is not None:
        return failed(f"the start fragment references another service ({other})", start_event)

    probes = [
        event
        for event in interval.events
        if event.action == "probe" and event.observed_state == "up" and event.request_span
    ]
    if not probes:
        return failed("the interval has no independently executable readiness probe", interval.probe_event)
    first_reason = None
    for probe_event in probes:
        probe_call = calls.get(probe_event.execution_id)
        if probe_call is None:
            reason = "the recipe references a call outside the analyzed timeline"
        else:
            outcome = _readiness(
                service, start_quote, start_call, probe_event, probe_call, services, timeout_sec
            )
            if isinstance(outcome, ServiceLaunch):
                launch = outcome
                return IntervalRecipe(
                    service=service.name,
                    interval=interval,
                    launch=launch,
                    unknowns=(),
                    probe_event=probe_event,
                )
            reason = outcome
        if first_reason is None:
            first_reason = reason
    return failed(first_reason or "no usable readiness probe", interval.probe_event, probes[0])


def build_recipes(
    timeline: Timeline,
    lifecycles: tuple[ServiceLifecycle, ...],
    services: tuple[ServiceEvidence, ...],
    *,
    timeout_sec: float,
) -> tuple[IntervalRecipe, ...]:
    """Build one recipe per confirmed interval; never invent missing commands."""
    calls = {call.execution_id: call for call in timeline.calls}
    recipes = []
    for lifecycle in lifecycles:
        for interval in lifecycle.intervals:
            recipes.append(
                _recipe(lifecycle.service, interval, services, calls, timeout_sec)
            )
    return tuple(recipes)
