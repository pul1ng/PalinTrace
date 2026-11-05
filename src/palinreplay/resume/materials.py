"""Read the exact selected Resume boundary without touching its external pool."""

from palinreplay.core.errors import IntegrityError
from palinreplay.record.restore import RestoreMaterials, load_restore_materials
from palinreplay.resume.start import ResumeStart
from palinreplay.storage.store import RecordStore


def load_resume_materials(start: ResumeStart) -> RestoreMaterials:
    """Check pinned metadata, all required bytes and JSON before creating resources."""
    with RecordStore.open(start.source_store, read_only=True) as store:
        with store.snapshot():
            materials = load_restore_materials(
                store, start.node.node_id, run_id=start.node.run_id,
            )
            if materials.node != start.node:
                raise IntegrityError("selected Resume node or material references changed")
            if (materials.run.run_id != start.node.run_id
                    or materials.state.state_id != start.node.state_id):
                raise IntegrityError("selected Resume Run/State identity changed")
            # The shared loader deliberately leaves these opaque. Resume needs
            # JSON objects, so force decoding now, before any Docker operation.
            for role in ("config", "context", "input_position"):
                if not isinstance(getattr(materials, role), dict):
                    raise IntegrityError(f"Resume {role} material must be a JSON object")
            return materials
