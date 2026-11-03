"""Pin one complete committed Run for preprocessing."""

from dataclasses import dataclass

from palinreplay.core.errors import RecordUsageError
from palinreplay.core.models import Node, Run
from palinreplay.replay.plan import ReplayPlan, pin_replay
from palinreplay.storage.store import RecordStore


@dataclass(frozen=True)
class PinnedSource:
    """An immutable, fully verified view of one Run's committed node chain."""

    plan: ReplayPlan

    @property
    def run(self) -> Run:
        return self.plan.run

    @property
    def nodes(self) -> tuple[Node, ...]:
        return self.plan.nodes

    @property
    def start_index(self) -> int:
        return self.plan.start.index

    @property
    def end_index(self) -> int:
        return self.plan.end.index

    def document(self) -> dict:
        provenance = self.plan.provenance()
        return {
            "run_id": provenance["run"]["run_id"],
            "start_index": provenance["nodes"][0]["index"],
            "end_index": provenance["nodes"][-1]["index"],
            "digest_scheme": provenance["digest_scheme"],
            "selection_sha256": provenance["selection_sha256"],
        }


def _select_run(store: RecordStore, run_id: str | None) -> Run:
    runs = store.list(Run)
    if run_id is not None:
        run = next((item for item in runs if item.run_id == run_id), None)
        if run is None:
            raise RecordUsageError(f"Run {run_id!r} is not present in this Record")
        return run
    if not runs:
        raise RecordUsageError("the Record contains no Run")
    if len(runs) != 1:
        identities = ", ".join(item.run_id for item in runs)
        raise RecordUsageError(
            f"the Record contains {len(runs)} Runs ({identities}); select one with --run"
        )
    return runs[0]


def pin_source(store: RecordStore, run_id: str | None = None) -> PinnedSource:
    """Freeze C0 through the last committed node, ignoring incomplete tails.

    ``pin_replay`` performs the contiguous-chain, state and full-range provenance
    checks. A Run without its own C0 therefore fails instead of producing a
    partial analysis range.
    """
    with store.snapshot():
        run = _select_run(store, run_id)
        plan = pin_replay(store, "C0", run_id=run.run_id)
        if plan.run != run:
            raise RecordUsageError("selected Run changed while pinning the source")
        if plan.start.index != 0:
            raise RecordUsageError("the selected Run does not start at C0")
    return PinnedSource(plan)
