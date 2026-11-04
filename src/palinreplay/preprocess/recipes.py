"""Extract evidenced, independently executable original startup commands."""

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

from pydantic import Field, StrictStr, field_validator

from palinreplay.core.schema import Spec
from palinreplay.preprocess.evidence import ServiceEvidence
from palinreplay.preprocess.lifecycle import ServiceInterval, ServiceLifecycle
from palinreplay.preprocess.shell import exact_segment, split_top_level  # noqa: F401
from palinreplay.preprocess.timeline import Timeline

STRONG_KINDS = frozenset({"endpoint", "program", "config", "instance"})
VAR_PATTERN = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


class ServiceLaunch(Spec):
    start_command: StrictStr
    cwd: StrictStr
    env: dict[StrictStr, StrictStr]
    timeout_sec: float = Field(gt=0, allow_inf_nan=False)

    @field_validator("start_command")
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

    @field_validator("env")
    @classmethod
    def environment(cls, value):
        if any(not key or "=" in key or "\x00" in key + content
               for key, content in value.items()):
            raise ValueError("launch env must contain valid keys and values")
        return value


@dataclass(frozen=True)
class IntervalRecipe:
    service: str
    interval: ServiceInterval
    launch: ServiceLaunch

    def document(self) -> dict:
        return {
            "service": self.service,
            "start_inclusive": self.interval.start_inclusive,
            "end_exclusive": self.interval.end_exclusive,
            "launch": self.launch.model_dump(mode="json"),
        }


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


def build_recipes(
    timeline: Timeline,
    lifecycles: tuple[ServiceLifecycle, ...],
    services: tuple[ServiceEvidence, ...],
    *,
    timeout_sec: float,
) -> tuple[IntervalRecipe, ...]:
    """Build startup recipes or fail with source references; never publish launch:null."""
    calls = {call.execution_id: call for call in timeline.calls}
    recipes = []
    for lifecycle in lifecycles:
        service = lifecycle.service
        for interval in lifecycle.intervals:
            event = interval.start_event

            def fail(reason):
                raise ValueError(
                    f"{service.name} C{event.node_index}/{event.execution_id}: {reason}"
                )

            call = calls.get(event.execution_id)
            if call is None or call.node_index != event.node_index:
                fail("the recipe references a call outside the analyzed timeline")
            if event.action != "start" or event.request_span is None or not event.executable:
                fail("the start fragment was not verified as independently executable")
            command = exact_segment(call.request.command, event.request_span.quote)
            if command is None:
                fail("the start quote is not a complete top-level command fragment")
            if event.dependencies:
                program = shlex.split(command)[0]
                program_names = {program}
                if "/" in program:
                    program_names.add(str(PurePosixPath(call.request.cwd, program)))
                unresolved = [dep for dep in event.dependencies if dep not in program_names]
                if unresolved:
                    fail("the start fragment has unresolved dependencies: " + ", ".join(unresolved))
            other = _mentions_other_service(command, service, services)
            if other is not None:
                fail(f"the start fragment references another service ({other})")
            if _referenced_env(command, call.request.env) is None:
                fail("a command references an environment variable absent from the source call")
            # Retain all recorded overrides: programs can read env without shell expansion.
            launch = ServiceLaunch(
                start_command=command, cwd=call.request.cwd, env=dict(call.request.env),
                timeout_sec=timeout_sec,
            )
            recipes.append(IntervalRecipe(service.name, interval, launch))
    return tuple(recipes)
