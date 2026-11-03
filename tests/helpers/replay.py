"""Complete incremental-comparison evidence shared by replay policy tests."""

import hashlib


def mtime_difference(path="file"):
    source = {"type": "file", "mode": 0o644, "uid": 0, "gid": 0,
              "size": 4, "sha256": hashlib.sha256(b"data").hexdigest(), "mtime_sec": 100}
    return {
        "policy": "rootfs-files-v1-strict-mtime-seconds", "equal": False,
        "different_path_count": 1, "different_paths": [path],
        "hardlinks_equal": True, "hardlink_paths": [],
        "differences": [{"path": path, "source": source, "actual": {**source, "mtime_sec": 200}}],
    }
