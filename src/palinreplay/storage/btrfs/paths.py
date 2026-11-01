"""Project stream-time names into the two snapshot endpoint namespaces."""

import stat
from pathlib import Path

CREATES = {"mkfile", "mkdir", "mknod", "mkfifo", "mksock", "symlink", "link"}


def under(path, root):
    return path == root or path.startswith(root + "/")


def moved(path, source, destination):
    return destination + path[len(source):]


def project(path, operations, *, reverse=False):
    """Track a name's lifetime; a deleted/replaced name has no endpoint path."""
    for op in reversed(operations) if reverse else operations:
        kind, name = op["command"], op.get("path")
        if kind == "rename":
            source, destination = (op["to"], name) if reverse else (name, op["to"])
            if under(path, source):
                path = moved(path, source, destination)
            elif under(path, destination):
                return None
        elif reverse and kind in CREATES and under(path, name):
            return None
        elif not reverse and kind in {"unlink", "rmdir"} and under(path, name):
            return None
    return path


def endpoint_path(root, name):
    """lstat-style traversal: never follow intermediate symlinks outside a snapshot."""
    current = Path(root)
    parts = [] if name == "." else name.split("/")
    for part in parts[:-1]:
        current /= part
        try:
            if not stat.S_ISDIR(current.lstat().st_mode):
                return None
        except FileNotFoundError:
            return None
    path = current / parts[-1] if parts else current
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    return path


def descendants(root, name):
    pending = [name]
    while pending:
        item = pending.pop()
        path = endpoint_path(root, item)
        if path is None:
            continue
        yield item
        if stat.S_ISDIR(path.lstat().st_mode):
            pending.extend((child.name if item == "." else item + "/" + child.name)
                           for child in path.iterdir())


def candidates(operations, before, after):
    result = set()
    for index, op in enumerate(operations):
        kind = op["command"]
        if kind in {"snapshot", "end"}:
            continue
        # The operation's source is in the namespace before it, destination in
        # the namespace after it. New names have no pre-creation identity.
        seeds = [(op["path"], index)]
        if kind in CREATES:
            seeds = [(op["path"], index + 1)]
        if kind == "rename":
            seeds.append((op["to"], index + 1))
        if kind == "link":
            seeds.append((op["link"], index))
        for name, cursor in seeds:
            for root, reverse, sequence in ((before, True, operations[:cursor]),
                                            (after, False, operations[cursor:])):
                endpoint = project(name, sequence, reverse=reverse)
                if endpoint is None or endpoint_path(root, endpoint) is None:
                    continue
                if kind in {"rename", "rmdir", "mkdir"}:
                    result.update(descendants(root, endpoint))
                else:
                    result.add(endpoint)
    return sorted(result)
