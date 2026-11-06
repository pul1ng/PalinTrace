from palinreplay.core.models import Run
from palinreplay.record.inspect import storage_metrics, store_file_bytes
from palinreplay.storage.payloads import ROOT_MEDIA
from palinreplay.storage.store import RecordStore


def test_directory_size_matches_full_metrics_without_reading_materials(tmp_path, monkeypatch):
    with RecordStore.create(tmp_path / "record") as store:
        ref = store.put_payload_json({"messages": ["history" * 2000] * 40})
        assert ref.media_type == ROOT_MEDIA
        store.save(Run(task_id="fixture", agent_name="fixture", agent_version="1", config=ref))
        (store.root / "artifacts/.tmp/pending").write_bytes(b"pending bytes")
        (store.root / "empty").mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "file").write_bytes(b"not part of this Store" * 100)
        (store.root / "linked-file").symlink_to(outside / "file")
        (store.root / "linked-directory").symlink_to(outside, target_is_directory=True)
        expected = storage_metrics(store)["store_file_bytes"]

        def no_material_reads(*args, **kwargs):
            raise AssertionError("directory accounting must not inspect trajectory materials")

        for name in ("list", "read_bytes", "read_payload", "iter_payload_artifacts"):
            monkeypatch.setattr(store, name, no_material_reads)
        assert store_file_bytes(store) == expected
        extra = store.root / "new-file"
        extra.write_bytes(b"12345")
        assert store_file_bytes(store) == expected + 5
        extra.unlink()
        assert store_file_bytes(store) == expected
