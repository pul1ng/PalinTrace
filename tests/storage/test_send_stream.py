import struct

import pytest

from palinreplay.storage.btrfs.send_stream import MAGIC, crc32c, parse_stream


def command(kind, attrs=()):
    payload = b"".join(struct.pack("<HH", key, len(value)) + value for key, value in attrs)
    header = struct.pack("<IHI", len(payload), kind, 0)
    crc = crc32c(header + payload)
    return struct.pack("<IHI", len(payload), kind, crc) + payload


def stream(*commands):
    first = command(2, [(1, bytes(16)), (2, bytes(8)), (15, b"child"),
                        (20, bytes([1]) * 16), (21, bytes(8))])
    return MAGIC + struct.pack("<I", 1) + first + b"".join(commands) + command(21)


def test_complete_no_data_changes():
    data = stream(command(3, [(3, bytes(8)), (15, b"file")]),
                  command(22, [(4, bytes(8)), (15, b"file"), (18, bytes(8))]),
                  command(17, [(4, bytes(8)), (15, b"file")]),
                  command(11, [(15, b"gone")]))
    ops = parse_stream(data)
    assert [op["command"] for op in ops] == [
        "snapshot", "mkfile", "update_extent", "truncate", "unlink", "end",
    ]


@pytest.mark.parametrize("change", [
    lambda d: d[:-1], lambda d: d[:-10], lambda d: d + b"junk",
    lambda d: d[:20] + bytes([d[20] ^ 1]) + d[21:],
    lambda d: MAGIC + struct.pack("<I", 2) + d[len(MAGIC) + 4:],
    lambda d: stream(command(99)), lambda d: stream(command(11)),
])
def test_invalid_stream_is_not_no_changes(change):
    with pytest.raises(ValueError):
        parse_stream(change(stream()))
