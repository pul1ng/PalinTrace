"""Find hardlink aliases without reading the tree's file contents."""

import ctypes
import fcntl
import os
import stat
import struct

if __package__:
    from .paths import endpoint_path
else:
    from paths import endpoint_path

# _IOWR(BTRFS_IOCTL_MAGIC, 35, struct btrfs_ioctl_ino_path_args)
INO_PATHS = 0xC0389423


def inode_paths(root, inode):
    size = 65536
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        while True:
            buffer = ctypes.create_string_buffer(size)
            args = struct.pack("7Q", inode, size, 0, 0, 0, 0, ctypes.addressof(buffer))
            fcntl.ioctl(fd, INO_PATHS, args)
            _, missing, count, missed = struct.unpack_from("4I", buffer.raw)
            if missing or missed:
                if not missing:
                    raise ValueError("incomplete Btrfs inode path lookup")
                size += missing
                continue
            paths = []
            for index in range(count):
                offset = 16 + struct.unpack_from("Q", buffer.raw, 16 + index * 8)[0]
                if offset >= size or b"\0" not in buffer.raw[offset:]:
                    raise ValueError("invalid Btrfs inode path response")
                name = os.fsdecode(buffer.raw[offset:].split(b"\0", 1)[0])
                if not name or name.startswith("/") or ".." in name.split("/"):
                    raise ValueError("invalid Btrfs inode alias")
                paths.append(name)
            return paths
    finally:
        os.close(fd)


def close_links(roots, names):
    result, pending, seen = set(names), list(names), set()
    while pending:
        name = pending.pop()
        for root in roots:
            path = endpoint_path(root, name)
            if path is None:
                continue
            info = path.lstat()
            key = (str(root), info.st_ino)
            if key in seen or not stat.S_ISREG(info.st_mode) or info.st_nlink < 2:
                continue
            seen.add(key)
            aliases = inode_paths(root, info.st_ino)
            for alias in aliases:
                check = endpoint_path(root, alias)
                if check is None or check.lstat().st_ino != info.st_ino:
                    raise ValueError("Btrfs inode alias disagrees with immutable snapshot")
                if alias not in result:
                    result.add(alias)
                    pending.append(alias)
    return sorted(result)
