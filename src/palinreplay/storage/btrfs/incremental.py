"""Candidate-only rootfs-files-v1 comparison, shared with the Linux worker."""

import hashlib
import os
import stat
import time

if __package__:
    from .links import close_links
    from .paths import endpoint_path
else:
    from links import close_links
    from paths import endpoint_path


def included(name, scope):
    return name not in scope["exclude_paths"] and not any(
        name == p or name.startswith(p + "/") for p in scope["exclude_subtrees"]
    )


def selected_inventory(root, names, scope):
    entries, groups, read_bytes = {}, {}, 0
    hashes = {}
    for name in sorted(names):
        if not included(name, scope):
            continue
        path = endpoint_path(root, name)
        if path is None:
            continue
        s = path.lstat()
        item = {"mode": stat.S_IMODE(s.st_mode), "uid": s.st_uid, "gid": s.st_gid}
        if stat.S_ISLNK(s.st_mode):
            item.update(type="symlink", target=os.readlink(path))
        elif stat.S_ISDIR(s.st_mode):
            item["type"] = "directory"
        elif stat.S_ISREG(s.st_mode):
            key = (s.st_dev, s.st_ino)
            if key not in hashes:
                h = hashlib.sha256()
                with path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        h.update(chunk)
                        read_bytes += len(chunk)
                hashes[key] = h.hexdigest()
            item.update(type="file", size=s.st_size, sha256=hashes[key], mtime_sec=int(s.st_mtime))
            groups.setdefault(key, []).append(name)
        else:
            raise ValueError("unsupported file type in the managed scope")
        entries[name] = item
    return {
        "schema_version": "palinreplay.rootfs-manifest.v1", "scope": scope,
        "entries": entries,
        "hardlink_groups": sorted(sorted(group) for group in groups.values() if len(group) > 1),
    }, read_bytes


def compare_candidates(source, actual, names, scope):
    started = time.monotonic()
    names = close_links((source, actual), names)
    names = [name for name in names if included(name, scope)]
    left, left_bytes = selected_inventory(source, names, scope)
    right, right_bytes = selected_inventory(actual, names, scope)
    paths = sorted(p for p in names if left["entries"].get(p) != right["entries"].get(p))
    link_paths = sorted({p for group in left["hardlink_groups"] + right["hardlink_groups"]
                         if group not in left["hardlink_groups"] or group not in right["hardlink_groups"]
                         for p in group})
    return {
        "scope": scope["id"], "policy": "rootfs-files-v1-strict-mtime-seconds",
        "equal": not paths and not link_paths,
        "different_path_count": len(paths), "different_paths": paths,
        "hardlinks_equal": not link_paths, "hardlink_paths": link_paths,
        "differences": [{"path": p, "source": left["entries"].get(p),
                         "actual": right["entries"].get(p)} for p in paths],
        "candidate_paths": names, "candidate_path_count": len(names),
        "read_bytes": left_bytes + right_bytes, "source_read_bytes": left_bytes,
        "actual_read_bytes": right_bytes, "comparison_sec": time.monotonic() - started,
    }
