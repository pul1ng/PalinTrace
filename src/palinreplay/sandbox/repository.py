"""Preserve-image repository contract: observe the image, never initialize by writing.

The selected immutable image is the runtime baseline, not an automatically
approved patch to the task. Git ancestry/diffs describe provenance; they do not
prove that preparation changes are semantically harmless.
"""

import hashlib
import io
import re
import shlex
import tarfile
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from palinreplay.sandbox.base import EnvironmentMismatch, ExecResult, RepositoryChecks

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_MAX_FILE = 4 * 1024**2
_MAX_ARCHIVE = _MAX_FILE + 256 * 1024


def _oid(value: str) -> str:
    if not _OID.fullmatch(value):
        raise EnvironmentMismatch(f"expected a full Git object ID, got {value!r}")
    return value


def _image_file(container: Any, path: str) -> bytes:
    """Read one bounded regular file via Docker, without starting or extracting it."""
    stream, _ = container.get_archive(path)
    data = bytearray()
    try:
        for chunk in stream:
            data.extend(chunk)
            if len(data) > _MAX_ARCHIVE:
                raise EnvironmentMismatch(f"image Git metadata is too large: {path}")
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            members = archive.getmembers()
            if (
                len(members) != 1
                or not members[0].isreg()
                or members[0].name != PurePosixPath(path).name
                or not 0 <= members[0].size <= _MAX_FILE
            ):
                raise EnvironmentMismatch(f"expected one regular image Git metadata file: {path}")
            handle = archive.extractfile(members[0])
            if handle is None:
                raise EnvironmentMismatch(f"cannot read image Git metadata: {path}")
            return handle.read()
    except tarfile.TarError as exc:
        raise EnvironmentMismatch(f"invalid image Git metadata archive: {path}") from exc


def read_image_head(container: Any, workdir: str) -> str:
    """Call only between containers.create() and start(), before BASH_ENV can run.

    This release supports ordinary .git directories, detached HEAD, loose refs
    and packed refs. Missing metadata/history is unsupported, never guessed.
    """
    git_dir = PurePosixPath(workdir) / ".git"
    try:
        head = _image_file(container, str(git_dir / "HEAD")).decode("utf-8").strip()
        if not head.startswith("ref: "):
            return _oid(head)
        ref = head.removeprefix("ref: ")
        # Resolve only normal, safe refs; do not follow arbitrary paths/symrefs.
        if (
            not re.fullmatch(r"refs/[A-Za-z0-9._/-]+", ref)
            or any(part in {"", ".", ".."} for part in ref.split("/"))
            or ".." in ref
        ):
            raise EnvironmentMismatch(f"unsupported image Git HEAD reference: {ref!r}")
        try:
            return _oid(_image_file(container, str(git_dir / ref)).decode("ascii").strip())
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
        packed = _image_file(container, str(git_dir / "packed-refs")).decode("utf-8")
        matches = [
            line.split()[0]
            for line in packed.splitlines()
            if not line.startswith(("#", "^"))
            and len(line.split()) == 2
            and line.split()[1] == ref
        ]
        if len(matches) != 1:
            raise EnvironmentMismatch(f"cannot uniquely resolve image Git HEAD reference: {ref}")
        return _oid(matches[0])
    except EnvironmentMismatch:
        raise
    except Exception as exc:
        raise EnvironmentMismatch(f"cannot read prepared image Git HEAD at {git_dir}: {exc}") from exc


def _diff_counts(raw: bytes) -> tuple[int, int, int]:
    """Parse --raw -z --no-renames without decoding potentially unusual paths."""
    if not raw:
        return 0, 0, 0
    parts = raw.split(b"\0")
    if parts[-1] or len(parts) % 2 != 1:
        raise EnvironmentMismatch("invalid repository raw diff")
    mode_only = 0
    for header, path in zip(parts[:-1:2], parts[1::2], strict=True):
        fields = header.split()
        if (
            len(fields) != 5
            or not re.fullmatch(rb":[0-7]{6}", fields[0])
            or not re.fullmatch(rb"[0-7]{6}", fields[1])
            or not re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", fields[2])
            or not re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", fields[3])
            or fields[4] not in {b"A", b"D", b"M", b"T"}
            or not path
        ):
            raise EnvironmentMismatch("invalid repository raw diff entry")
        if fields[2] == fields[3] and fields[0][1:] != fields[1]:
            mode_only += 1
    total = (len(parts) - 1) // 2
    return total, mode_only, total - mode_only


def verify_repository(
    base_commit: str,
    image_head: str,
    checked: Callable[[str], ExecResult],
) -> RepositoryChecks:
    """Verify preservation and source lineage; retain raw probe evidence upstream."""
    base_commit, image_head = _oid(base_commit), _oid(image_head)

    def git(*args: str) -> ExecResult:
        return checked(shlex.join(["git", *args]))

    head = _oid(git("rev-parse", "HEAD").text.strip())
    if head != image_head:
        raise EnvironmentMismatch(
            f"prepared image HEAD mismatch: expected {image_head}, got {head}; "
            "preserve-image does not checkout the task base commit"
        )
    resolved = git("rev-parse", "--verify", "--end-of-options", f"{base_commit}^{{commit}}")
    if resolved.text.strip() != base_commit:
        raise EnvironmentMismatch("task base commit does not resolve to its original commit")
    # This is an additional provenance check, NOT permission to accept any
    # descendant: the independent pre-start image_head check above is mandatory.
    git("merge-base", "--is-ancestor", base_commit, head)
    count = git("rev-list", "--count", f"{base_commit}..{head}").text.strip()
    if not count.isascii() or not count.isdigit() or (int(count) == 0) != (head == base_commit):
        raise EnvironmentMismatch("invalid repository ancestry count")
    tree = _oid(git("rev-parse", "--verify", "--end-of-options", f"{head}^{{tree}}").text.strip())
    diff = git(
        "diff", "--raw", "-z", "--no-abbrev", "--no-renames", "--no-relative",
        "--no-ext-diff", "--no-textconv", base_commit, head, "--",
    ).output
    total, mode_only, content = _diff_counts(diff)
    # Check staged/unstaged tracked contents AND executable bits. Ignored build
    # products/untracked files belong to the image, not to this Git-only check.
    git("-c", "core.fileMode=true", "diff", "--no-ext-diff", "--no-textconv", "--quiet", "HEAD", "--")
    if git("rev-parse", "HEAD").text.strip() != image_head:
        raise EnvironmentMismatch("repository HEAD changed during startup verification")
    return RepositoryChecks(
        base_commit=base_commit,
        image_head=image_head,
        head=head,
        tree=tree,
        relation="same" if head == base_commit else "descendant",
        commits_ahead=int(count),
        diff_sha256=hashlib.sha256(diff).hexdigest(),
        changed_files=total,
        mode_only_files=mode_only,
        content_changed_files=content,
        tracked_worktree_clean=True,
    )
