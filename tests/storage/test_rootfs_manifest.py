import io
import tarfile

import pytest

import palinreplay.storage.rootfs as _rootfs
from palinreplay.storage.store import IntegrityError, RecordStore

SOURCE = "c" * 64


def tar_bytes(members):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for name, kind, value, mode in members:
            member = tarfile.TarInfo(name)
            member.type, member.mode = kind, mode
            member.uid, member.gid, member.mtime = 1234, 2345, 1700000000
            if kind == tarfile.REGTYPE:
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
            else:
                if kind in (tarfile.LNKTYPE, tarfile.SYMTYPE):
                    member.linkname = value
                archive.addfile(member)
    return data.getvalue()


def files(value=b"initial\x00\xff\r\n", mode=0o640):
    return [
        ("testbed", tarfile.DIRTYPE, "", 0o755),
        ("testbed/a", tarfile.REGTYPE, value, mode),
        ("testbed/link", tarfile.SYMTYPE, "a", 0o777),
        ("testbed/hard", tarfile.LNKTYPE, "testbed/a", mode),
        ("testbed/.git/info/exclude", tarfile.REGTYPE, b"ignored", 0o644),
        ("opt/config", tarfile.REGTYPE, value, 0o600),
        ("tmp/empty", tarfile.DIRTYPE, "", 0o700),
    ]


def test_streamed_artifact_copy_verifies_bytes_and_preserves_existing_file(tmp_path):
    data = b"\x00\xff\r\n" * (1024 * 1024)
    with RecordStore.create(tmp_path / "record") as store:
        ref = store.put_bytes(data)
    with RecordStore.open(tmp_path / "record") as store:
        destination = tmp_path / "copy"
        store.export_artifact(ref, destination)
        assert destination.read_bytes() == data
        with pytest.raises(FileExistsError):
            store.export_artifact(ref, destination)
        wrong = ref.model_copy(update={"size": ref.size + 1})
        with pytest.raises(IntegrityError):
            store.export_artifact(wrong, tmp_path / "partial")
        assert not (tmp_path / "partial").exists()
        assert destination.read_bytes() == data


@pytest.mark.parametrize(
    "members",
    [
        [("../escape", tarfile.REGTYPE, b"bad", 0o644)],
        [("/absolute", tarfile.REGTYPE, b"bad", 0o644)],
        [("a", tarfile.REGTYPE, b"one", 0o644), ("./a", tarfile.REGTYPE, b"two", 0o644)],
        [(".wh.deleted", tarfile.REGTYPE, b"", 0o644)],
        [("a", tarfile.SYMTYPE, "/outside", 0o777), ("a/child", tarfile.REGTYPE, b"bad", 0o644)],
        [("a", tarfile.LNKTYPE, "missing", 0o644)],
        [("a", tarfile.LNKTYPE, "b", 0o644), ("b", tarfile.LNKTYPE, "a", 0o644)],
        [("a", tarfile.LNKTYPE, "proc/data", 0o644), ("proc/data", tarfile.REGTYPE, b"x", 0o644)],
        [("tmp/pipe", tarfile.FIFOTYPE, "", 0o600)],
    ],
)
def test_unsafe_or_unrepresentable_rootfs_is_rejected_without_extraction(tmp_path, members):
    path = tmp_path / "rootfs.tar"
    path.write_bytes(tar_bytes(members))
    with pytest.raises(ValueError):
        _rootfs.manifest(path)


def test_complete_archive_markers_and_trailing_data_are_checked(tmp_path):
    path = tmp_path / "rootfs.tar"
    full = tar_bytes(files())
    for content in (full[:-1], full[:512], full + tar_bytes(files(b"hidden appended archive"))):
        path.write_bytes(content)
        with pytest.raises((ValueError, tarfile.TarError)):
            _rootfs.manifest(path)


def test_hardlink_layout_and_ignored_attributes_compare_by_logical_state(tmp_path):
    first, second, third = [tmp_path / name for name in ("one.tar", "two.tar", "three.tar")]
    first.write_bytes(
        tar_bytes(
            [
                ("a", tarfile.REGTYPE, b"same", 0o640),
                ("b", tarfile.LNKTYPE, "a", 0o640),
                ("proc/ignored", tarfile.FIFOTYPE, "", 0o600),
            ]
        )
    )
    second.write_bytes(
        tar_bytes(
            [
                ("b", tarfile.REGTYPE, b"same", 0o640),
                ("a", tarfile.LNKTYPE, "b", 0o640),
            ]
        )
    )
    third.write_bytes(
        tar_bytes(
            [
                ("a", tarfile.REGTYPE, b"same", 0o640),
                ("b", tarfile.REGTYPE, b"same", 0o640),
            ]
        )
    )
    one, two, three = map(_rootfs.manifest, (first, second, third))
    assert _rootfs.compare(one, two)["equal"]
    comparison = _rootfs.compare(one, three)
    assert comparison["different_path_count"] == 0 and not comparison["hardlinks_equal"]
