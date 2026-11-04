import pytest

from examples import resume_start_validation as validation
from palinreplay.core.errors import RecordUsageError
from palinreplay.storage.store import RecordStore


@pytest.mark.parametrize("source_kind", ["recording", "store"])
@pytest.mark.parametrize("output_kind", ["inside", "symlink"])
def test_validation_rejects_output_inside_source_before_writing(tmp_path, source_kind, output_kind):
    recording = tmp_path / "run"
    store = recording / "record"
    with RecordStore.create(store):
        pass
    source = recording if source_kind == "recording" else store
    output_parent = store
    if output_kind == "symlink":
        output_parent = tmp_path / "store-link"
        output_parent.symlink_to(store, target_is_directory=True)
    output = output_parent / "validation"
    before = set(recording.rglob("*"))
    catalog = (store / "catalog.sqlite").read_bytes()

    with pytest.raises(RecordUsageError, match="output must be outside the source Record store"):
        validation.main(source, output)

    assert not output.exists()
    assert set(recording.rglob("*")) == before
    assert (store / "catalog.sqlite").read_bytes() == catalog
