"""Strict Btrfs send protocol v1 parser (also shipped to the Linux worker)."""

import os
import struct

MAGIC = b"btrfs-stream\0"
COMMANDS = {
    1: "subvol", 2: "snapshot", 3: "mkfile", 4: "mkdir", 5: "mknod",
    6: "mkfifo", 7: "mksock", 8: "symlink", 9: "rename", 10: "link",
    11: "unlink", 12: "rmdir", 13: "set_xattr", 14: "remove_xattr", 15: "write",
    16: "clone", 17: "truncate", 18: "chmod", 19: "chown", 20: "utimes",
    21: "end", 22: "update_extent",
}
REQUIRED = {
    2: (1, 2, 15, 20, 21), 3: (3, 15), 4: (3, 15), 5: (3, 5, 8, 15),
    6: (3, 15), 7: (3, 15), 8: (3, 15, 17), 9: (15, 16), 10: (15, 17),
    11: (15,), 12: (15,), 13: (13, 14, 15), 14: (13, 15),
    17: (4, 15), 18: (5, 15), 19: (6, 7, 15), 20: (9, 10, 11, 15),
    21: (), 22: (4, 15, 18),
}


def crc32c(data):
    crc = 0
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc


def _path(value):
    path = os.fsdecode(value) or "."
    if (path.startswith("/") or "\0" in path
            or any(p in ("", "..") for p in path.split("/"))):
        raise ValueError("invalid send stream path")
    return path


def parse_stream(data):
    if data[:len(MAGIC)] != MAGIC or len(data) < len(MAGIC) + 4:
        raise ValueError("missing Btrfs stream header")
    if struct.unpack_from("<I", data, len(MAGIC))[0] != 1:
        raise ValueError("unsupported Btrfs send protocol version")
    offset, operations, ended = len(MAGIC) + 4, [], False
    while offset < len(data):
        if ended or len(data) - offset < 10:
            raise ValueError("trailing or truncated Btrfs command header")
        size, command, checksum = struct.unpack_from("<IHI", data, offset)
        stop = offset + 10 + size
        if stop > len(data):
            raise ValueError("truncated Btrfs command")
        body = data[offset:offset + 6] + bytes(4) + data[offset + 10:stop]
        if crc32c(body) != checksum:
            raise ValueError("Btrfs send command checksum mismatch")
        if command not in REQUIRED:
            raise ValueError(f"unsupported no-data Btrfs command: {command}")
        attrs, pos = {}, offset + 10
        while pos < stop:
            if stop - pos < 4:
                raise ValueError("truncated Btrfs attribute")
            key, length = struct.unpack_from("<HH", data, pos)
            pos += 4
            if key not in range(1, 25) or key in attrs or pos + length > stop:
                raise ValueError("invalid Btrfs command attribute")
            attrs[key] = data[pos:pos + length]
            pos += length
        if any(key not in attrs for key in REQUIRED[command]):
            raise ValueError("missing Btrfs command attribute")
        for key, value in attrs.items():
            size = 8 if key in (2, 3, 4, 5, 6, 7, 8, 18, 21, 24) else None
            if key in (9, 10, 11, 12):
                size = 12
            if size is not None and len(value) != size:
                raise ValueError("invalid fixed-size Btrfs attribute")
        if (command == 2 and operations) or (command != 2 and not operations):
            raise ValueError("incremental stream must begin with one snapshot command")
        op = {"command": COMMANDS[command]}
        for key, name in ((15, "path"), (16, "to"), (17, "link")):
            if key in attrs:
                # Symlink targets are arbitrary strings, unlike filesystem paths.
                op[name] = os.fsdecode(attrs[key]) if command == 8 and key == 17 else _path(attrs[key])
        for key, name in ((1, "uuid"), (20, "parent_uuid")):
            if key in attrs:
                if len(attrs[key]) != 16:
                    raise ValueError("invalid Btrfs snapshot UUID")
                op[name] = attrs[key].hex()
        if command == 21:
            if attrs:
                raise ValueError("END command has attributes")
            ended = True
        operations.append(op)
        offset = stop
    if not ended:
        raise ValueError("Btrfs send stream has no END")
    return operations
