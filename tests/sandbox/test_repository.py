"""Offline tests: metadata archives and real Git, never a Docker daemon/network."""

import hashlib
import io
import os
import shlex
import subprocess
import tarfile

import pytest

from palinreplay.sandbox import EnvironmentMismatch, ExecResult
from palinreplay.sandbox.repository import _diff_counts, read_image_head, verify_repository


class Missing(Exception):
    status_code = 404


class ImageFiles:
    def __init__(self, files):
        self.files = files
        self.reads = []

    def get_archive(self, path):
        self.reads.append(path)
        if path not in self.files:
            raise Missing(path)
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            entry = tarfile.TarInfo(path.rsplit("/", 1)[-1])
            content = self.files[path]
            if isinstance(content, bytes):
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
            else:
                entry.type = tarfile.SYMTYPE
                entry.linkname = content
                archive.addfile(entry)
        return iter([data.getvalue()]), {}


@pytest.mark.parametrize("kind", ["detached", "loose", "packed", "sha256"])
def test_reads_image_head_without_executing_anything(kind):
    oid = "a" * (64 if kind == "sha256" else 40)
    files = {"/testbed/.git/HEAD": (oid + "\n").encode()}
    if kind in {"loose", "packed"}:
        files["/testbed/.git/HEAD"] = b"ref: refs/heads/main\n"
        if kind == "loose":
            files["/testbed/.git/refs/heads/main"] = (oid + "\n").encode()
        else:
            files["/testbed/.git/packed-refs"] = (
                f"# pack-refs with: peeled fully-peeled sorted\n{oid} refs/heads/main\n"
                f"{'b' * 40} refs/tags/v1\n^{'c' * 40}\n"
            ).encode()
    container = ImageFiles(files)
    assert read_image_head(container, "/testbed") == oid
    assert len(container.reads) == {"detached": 1, "sha256": 1, "loose": 2, "packed": 3}[kind]


@pytest.mark.parametrize("head", [
    b"ref: ../../etc/passwd\n", b"ref: refs/../../etc/passwd\n",
    b"ref: refs//heads/main\n", b"ref: refs/heads/main; echo bad\n",
    b"not-a-commit", b"a" * 39, b"\xff\xfe", "/outside/HEAD",
])
def test_rejects_unsafe_or_unsupported_image_head(head):
    container = ImageFiles({"/testbed/.git/HEAD": head})
    with pytest.raises(EnvironmentMismatch):
        read_image_head(container, "/testbed")
    assert container.reads == ["/testbed/.git/HEAD"]


@pytest.mark.parametrize("packed", [b"", b"a" * 40 + b" refs/heads/other\n", (
    ("a" * 40 + " refs/heads/main\n") * 2
).encode()])
def test_packed_ref_must_resolve_uniquely(packed):
    with pytest.raises(EnvironmentMismatch, match="uniquely resolve"):
        read_image_head(ImageFiles({
            "/testbed/.git/HEAD": b"ref: refs/heads/main\n",
            "/testbed/.git/packed-refs": packed,
        }), "/testbed")


def test_image_metadata_size_is_bounded_and_stream_closed(monkeypatch):
    monkeypatch.setattr("palinreplay.sandbox.repository._MAX_ARCHIVE", 10)
    closed = []

    class Container:
        def get_archive(self, path):
            def chunks():
                try:
                    yield b"x" * 11
                finally:
                    closed.append(True)
            return chunks(), {}

    with pytest.raises(EnvironmentMismatch, match="too large"):
        read_image_head(Container(), "/testbed")
    assert closed == [True]


@pytest.mark.parametrize("raw", [b"bad", b"bad\0path\0", b"bad\0", b"\0path\0"])
def test_invalid_raw_diff_is_not_silently_treated_as_empty(raw):
    with pytest.raises(EnvironmentMismatch, match="raw diff"):
        _diff_counts(raw)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # Do not inherit user Git hooks, signing, aliases, external diff or Git dirs.
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "0")
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "1")
    path = tmp_path / "repo"
    path.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        ).stdout.decode().strip()

    git("init", "--template=", "-b", "main")
    git("config", "user.name", "Offline test")
    git("config", "user.email", "offline@example.invalid")
    (path / "pyproject.toml").write_text('[build-system]\nrequires = ["setuptools"]\n')
    (path / "a file\nwith newline").write_bytes(b"unchanged\n")
    git("add", ".")
    git("commit", "-qm", "source")
    base = git("rev-parse", "HEAD")
    calls = []

    def checked(command):
        completed = subprocess.run(
            shlex.split(command), cwd=path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        result = ExecResult(command, completed.stdout, completed.returncode, 0)
        calls.append(result)
        if completed.returncode:
            raise EnvironmentMismatch(f"startup check failed: {command}\n{result.text}")
        return result

    return path, git, base, calls, checked


def test_real_git_same_commit_is_clean(repo):
    _, _, base, _, checked = repo
    state = verify_repository(base, base, checked)
    assert state.relation == "same" and state.commits_ahead == 0
    assert state.changed_files == state.mode_only_files == state.content_changed_files == 0
    assert state.diff_sha256 == hashlib.sha256(b"").hexdigest()


def test_real_git_preparation_diffs_and_weird_filenames_are_preserved(repo):
    path, git, base, calls, checked = repo
    (path / "a file\nwith newline").chmod(0o755)
    (path / "pyproject.toml").chmod(0o755)
    (path / "pyproject.toml").write_text('[build-system]\nrequires = ["setuptools==68.0.0"]\n')
    git("add", ".")
    git("commit", "-qm", "preparation")
    head = git("rev-parse", "HEAD")
    state = verify_repository(base, head, checked)
    assert state.base_commit == base and state.head == state.image_head == head
    assert state.relation == "descendant" and state.commits_ahead == 1
    assert state.changed_files == 2 and state.mode_only_files == state.content_changed_files == 1
    raw = next(c.output for c in calls if c.command.startswith("git diff --raw"))
    assert b"a file\nwith newline\0" in raw
    assert state.diff_sha256 == hashlib.sha256(raw).hexdigest()
    assert git("rev-parse", "HEAD") == head and git("status", "--porcelain") == ""


@pytest.mark.parametrize("kind", ["unstaged", "staged", "mode"])
def test_real_git_rejects_unexpected_tracked_changes_even_if_filemode_disabled(repo, kind):
    path, git, base, _, checked = repo
    if kind == "mode":
        git("config", "core.fileMode", "false")
        (path / "pyproject.toml").chmod(0o755)
    else:
        (path / "pyproject.toml").write_text("unexpected change")
        if kind == "staged":
            git("add", ".")
    with pytest.raises(EnvironmentMismatch, match="startup check failed"):
        verify_repository(base, base, checked)


def test_real_git_rejects_unrelated_image_history(repo):
    path, git, base, _, checked = repo
    git("checkout", "--orphan", "unrelated")
    (path / "unrelated").write_text("other history")
    git("add", ".")
    git("commit", "-qm", "unrelated root")
    with pytest.raises(EnvironmentMismatch, match="merge-base"):
        verify_repository(base, git("rev-parse", "HEAD"), checked)


def test_real_git_rejects_missing_base_without_fetch(repo):
    _, _, head, calls, checked = repo
    with pytest.raises(EnvironmentMismatch, match="rev-parse --verify"):
        verify_repository("f" * 40, head, checked)
    assert not any("fetch" in call.command for call in calls)
