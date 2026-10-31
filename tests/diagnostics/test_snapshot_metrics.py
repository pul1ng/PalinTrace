import pytest

from palinreplay.diagnostics.metrics import (
    boundary_summary,
    count,
    measure,
    phase,
    temporary_file,
    temporary_size,
)
from palinreplay.storage.store import RecordStore


def test_scopes_are_inclusive_isolated_and_cleaned_on_failure(tmp_path):
    with measure() as parent:
        with temporary_file(tmp_path / "first"):
            temporary_size(tmp_path / "first", 7)
            with pytest.raises(RuntimeError), measure() as child, phase("failed"):
                count(written_bytes=5)
                with temporary_file(tmp_path / "second"):
                    temporary_size(tmp_path / "second", 11)
                    raise RuntimeError("injected")
            assert parent.temporary_peak_bytes == 18
            assert child.temporary_peak_bytes == 11
            assert not child.temporary
        count(written_bytes=2)
    count(written_bytes=100)
    assert parent.snapshot()["written_bytes"] == 7
    assert child.snapshot()["written_bytes"] == 5
    assert parent.phases["failed"] == child.phases["failed"]
    assert not parent.temporary
    assert str(tmp_path) not in str(parent.snapshot())
    with measure() as fresh:
        assert fresh.snapshot()["written_bytes"] == 0


def test_real_copy_and_verification_count_logical_bytes(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"abc\x00\xff")
    with RecordStore.create(tmp_path / "store") as store, measure() as metrics:
        ref = store.put_file(source)
        first = metrics.snapshot()
        assert first["read_bytes"] == first["written_bytes"] == first["copied_bytes"] == 5
        assert first["hashed_bytes"] == first["temporary_peak_bytes"] == 5
        assert first["verification_calls"] == 0
        assert not metrics.temporary
        store.verify_artifact(ref)
        assert store.read_bytes(ref) == source.read_bytes()
        final = metrics.snapshot()
        assert final["read_bytes"] == final["hashed_bytes"] == 15
        assert final["verified_bytes"] == 10
        assert final["verification_calls"] == 2
        assert final["written_bytes"] == 5


def test_initial_and_steps_never_double_count_nested_metrics():
    with measure() as stats:
        count(read_bytes=11)
    rows = [
        {"index": 0, "duration_sec": 8, "io": stats.snapshot()},
        {"index": 1, "duration_sec": 3, "io": stats.snapshot()},
        {"index": 2, "duration_sec": 4, "io": stats.snapshot()},
    ]
    summary = boundary_summary(rows)
    assert summary["initial"]["duration_sec"] == 8
    assert summary["steps"]["duration_sec"] == 7
    assert summary["steps"]["io"]["read_bytes"] == 22
    assert boundary_summary([{"index": 0, "duration_sec": 8}])["initial"]["io"] is None
