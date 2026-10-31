"""Small Agent-facing contract. Importing it does not import any Agent SDK."""

from dataclasses import dataclass, field
from typing import Protocol

from palinreplay.core.interfaces import AgentSnapshot
from palinreplay.tasks.model import TaskPackage


@dataclass(frozen=True)
class AgentCapabilities:
    run: bool
    trajectory_export: bool
    record: bool
    context_restore: bool = False


@dataclass(frozen=True)
class AgentInfo:
    name: str
    version: str
    capabilities: AgentCapabilities


@dataclass(frozen=True)
class AgentTask:
    task_id: str
    instruction: str
    public_context: dict = field(default_factory=dict)

    @classmethod
    def from_package(cls, task: TaskPackage) -> "AgentTask":
        public = task.agent_input()
        return cls(public["task_id"], public["instruction"], {"environment": public["environment"]})


class AgentAdapter(Protocol):
    info: AgentInfo

    def run(self, task: AgentTask) -> dict: ...

    def export_snapshot(self) -> AgentSnapshot: ...


def get_agent_class(name: str) -> type:
    """Explicit, lazy registry; capabilities belong to each implementation."""
    if name == "mini-swe-agent":
        from palinreplay.agents.mini import MiniSweAgent

        return MiniSweAgent
    raise ValueError(f"unknown Agent adapter: {name}")
