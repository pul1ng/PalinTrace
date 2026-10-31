"""SWE-bench full test split: 2,294 instances."""

from palinreplay.tasks.sources.swebench.common import variant_spec

SPEC = variant_spec("full", aliases=("swe-bench",))
