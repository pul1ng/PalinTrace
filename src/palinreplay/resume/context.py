"""Saved conversation views; no native Agent initialization or budget rewriting."""

from dataclasses import dataclass

from palinreplay.record.restore import RestoreMaterials


@dataclass(frozen=True)
class ResumePrefix:
    """Expose the saved objects and retain their original encoded payloads."""

    materials: RestoreMaterials

    @property
    def context(self):
        return self.materials.context

    @property
    def messages(self):
        return self.context["messages"]

    @property
    def config(self):
        return self.context["config"]

    @property
    def counters(self):
        return self.context["counters"]

    @property
    def input_position(self):
        return self.materials.input_position
