"""Trusted native-Linux worker, copied into the owned Btrfs management container.

Only ordinary files in /storage and our mounted /pool are managed. Never format
a supplied block device. Inventory is reserved for preload/explicit restoration;
ordinary checkpoints only flush writes and create read-only CoW snapshots.
"""

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

POOL = Path("/pool")
STORAGE = Path("/storage")
DEV = Path("/kerneldev")
REQUESTS = Path("/tmp")


def read_request(arguments):
    if len(arguments) == 1:
        return json.loads(arguments[0])
    if (len(arguments) != 2 or arguments[0] != "--request-file"
            or not re.fullmatch(r"palin-btrfs-request-[0-9a-f]{32}\.json", arguments[1])):
        raise ValueError("invalid Btrfs worker request arguments")
    path = REQUESTS / arguments[1]
    try:
        return json.loads(path.read_bytes())
    finally:
        path.unlink(missing_ok=True)


def command(*args):
    result = subprocess.run(list(map(str, args)), capture_output=True, timeout=1200)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.decode(errors='replace')[-1500:]}")
    return result.stdout.decode()


def encoded(value):
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n"
    ).encode()


def save(path, value):
    with Path(path).open("wb") as f:
        f.write(encoded(value))
        f.flush()
        os.fsync(f.fileno())


def target(name):
    if not re.fullmatch(r"(bases/C0|workspaces/[0-9a-f]{32}|checkpoints/[0-9a-f]{32})", name):
        raise ValueError("invalid managed subvolume name")
    return POOL / name


def setup(request):
    for p in (POOL, DEV):
        p.mkdir(exist_ok=True)
    command("mount", "-t", "devtmpfs", "devtmpfs", DEV)
    image = STORAGE / "pool.img"
    if request["fresh"]:
        with image.open("xb") as f:
            f.truncate(16 * 1024**3)
        command("mkfs.btrfs", "-f", "-d", "single", "-m", "dup", "-U", request["uuid"], image)
    with (DEV / "loop-control").open("rb") as f:
        number = fcntl.ioctl(f, 0x4C82)
    device = DEV / f"loop{number}"
    canonical = Path("/dev") / device.name
    if not canonical.exists():
        canonical.symlink_to(device)
    # Record the device before mounting so failed setup can still clean it up.
    save(STORAGE / "mount.json", {"device": str(device), "mounted": False})
    command("losetup", device, image)
    command("mount", "-t", "btrfs", "-o", "noatime", device, POOL)
    save(STORAGE / "mount.json", {"device": str(device), "mounted": True})
    found = re.search(r"uuid:\s*([0-9a-f-]+)", command("btrfs", "filesystem", "show", POOL))
    if not found or found[1] != request["uuid"]:
        raise ValueError("Btrfs filesystem UUID differs from the selected pool")
    if request["fresh"]:
        for name in ("bases", "workspaces", "checkpoints"):
            (POOL / name).mkdir()
    return {"device": "/dev/" + device.name, "uuid": found[1]}


def close():
    if not (STORAGE / "mount.json").exists():
        if os.path.ismount(DEV):
            command("umount", DEV)
        return {"detached": True}
    data = json.loads((STORAGE / "mount.json").read_text())
    if os.path.ismount(POOL):
        command("sync", "-f", POOL)
        command("umount", POOL)
    # LOOP_CLR_FD is lazy while runtime-held mount references still exist.
    # The caller removes this container and independently confirms release.
    if command("losetup", "-j", STORAGE / "pool.img").strip():
        command("losetup", "-d", data["device"])
    if data["mounted"]:
        for device in ("/dev/" + Path(data["device"]).name, data["device"]):
            result = subprocess.run(
                ["btrfs", "device", "scan", "--forget", device], capture_output=True, timeout=30
            )
            if result.returncode and b"Device or resource busy" not in result.stderr:
                raise RuntimeError("cannot clear the owned Btrfs device registration")
    command("umount", DEV)
    (STORAGE / "mount.json").unlink()
    return {"unmounted": True, "loop_autoclear_requested": True}


def info(name):
    path = target(name)
    values = {}
    for line in command("btrfs", "subvolume", "show", path).splitlines():
        if ":" in line:
            k, v = line.strip().split(":", 1)
            values[k] = v.strip()
    if "UUID" not in values:
        raise ValueError("missing snapshot UUID")
    return {"name": name, "uuid": values["UUID"], "readonly": "readonly" in values["Flags"]}


def healthy():
    values = command("btrfs", "device", "stats", POOL)
    if any(int(line.rsplit(None, 1)[-1]) for line in values.splitlines() if line.strip()):
        raise OSError("Btrfs has recorded storage/checksum errors")


def inventory(root, scope, include_root=True):
    entries, groups = {}, {}
    pending = [(".", root)]
    while pending:
        name, path = pending.pop()
        if name in scope["exclude_paths"] or any(
            name == p or name.startswith(p + "/") for p in scope["exclude_subtrees"]
        ):
            continue
        s = path.lstat()
        item = {"mode": stat.S_IMODE(s.st_mode), "uid": s.st_uid, "gid": s.st_gid}
        if stat.S_ISLNK(s.st_mode):
            item.update(type="symlink", target=os.readlink(path))
        elif stat.S_ISDIR(s.st_mode):
            item["type"] = "directory"
            for child in os.scandir(path):
                pending.append(
                    (child.name if name == "." else name + "/" + child.name, Path(child.path))
                )
        elif stat.S_ISREG(s.st_mode):
            h = hashlib.sha256()
            with path.open("rb") as f:
                while chunk := f.read(1024 * 1024):
                    h.update(chunk)
            item.update(
                type="file", size=s.st_size, sha256=h.hexdigest(), mtime_sec=int(s.st_mtime)
            )
            groups.setdefault((s.st_dev, s.st_ino), []).append(name)
        else:
            raise ValueError("unsupported file type in the managed scope")
        if name != "." or include_root:
            entries[name] = item
    return {
        "schema_version": "palinreplay.rootfs-manifest.v1",
        "scope": scope,
        "entries": entries,
        "hardlink_groups": sorted(sorted(g) for g in groups.values() if len(g) > 1),
    }


def compare(left, right):
    changes = [
        p
        for p in left["entries"].keys() | right["entries"].keys()
        if left["entries"].get(p) != right["entries"].get(p)
    ]
    return {
        "equal": left == right,
        "different_path_count": len(changes),
        "different_paths": sorted(changes)[:30],
        "hardlinks_equal": left["hardlink_groups"] == right["hardlink_groups"],
    }


def operation(request):
    op = request["op"]
    if op == "setup":
        return setup(request)
    if op == "close":
        return close()
    if op not in ("delete", "usage"):
        healthy()
    if op == "info":
        return info(request["name"])
    if op == "import":
        root = target(request["workspace"])
        command("btrfs", "subvolume", "create", root)
        expected = json.loads((STORAGE / "expected-C0.json").read_text())
        try:
            command(
                "tar",
                "--extract",
                "--file=/storage/import.tar",
                "--same-owner",
                "--same-permissions",
                "--directory",
                root,
            )
        finally:
            (STORAGE / "import.tar").unlink(missing_ok=True)
        actual = inventory(root, expected["scope"], "." in expected["entries"])
        result = compare(expected, actual)
        if not result["equal"]:
            raise ValueError(f"import differs from original Docker export: {result}")
        return {
            "paths": len(actual["entries"]),
            "roundtrip_verified": True,
            "oracle_sha256": hashlib.sha256(encoded(expected)).hexdigest(),
        }
    if op == "snapshot":
        source, destination = target(request["source"]), target(request["destination"])
        # Nested subvolumes would be omitted by Btrfs snapshot semantics.
        if command("btrfs", "subvolume", "list", "-o", source).strip():
            raise ValueError("nested task subvolumes are outside the capture scope")
        command("sync", "-f", source)
        command(
            "btrfs",
            "subvolume",
            "snapshot",
            *(["-r"] if request["readonly"] else []),
            source,
            destination,
        )
        command("btrfs", "filesystem", "sync", POOL)
        return info(request["destination"])
    if op == "changes":
        # This module is explicitly shipped alongside the standalone worker.
        if __package__:
            from .links import close_links
            from .paths import candidates
            from .send_stream import parse_stream
        else:
            from links import close_links
            from paths import candidates
            from send_stream import parse_stream
        before, after = info(request["before"]), info(request["after"])
        if not before["readonly"] or not after["readonly"]:
            raise ValueError("incremental discovery requires read-only snapshots")
        result = subprocess.run(
            ["btrfs", "send", "--proto", "1", "--no-data", "-p",
             str(target(request["before"])), str(target(request["after"]))],
            capture_output=True, timeout=1200,
        )
        if result.returncode:
            raise ValueError("Btrfs no-data send failed: " + result.stderr.decode(errors="replace"))
        operations = parse_stream(result.stdout)
        from uuid import UUID
        if (operations[0]["uuid"] != UUID(after["uuid"]).hex
                or operations[0]["parent_uuid"] != UUID(before["uuid"]).hex):
            raise ValueError("incremental stream snapshot identity mismatch")
        roots = (target(request["before"]), target(request["after"]))
        names = close_links(roots, candidates(operations, *roots))
        return {"protocol": 1, "no_data": True, "complete": True,
                "stream_bytes": len(result.stdout), "operations": operations,
                "candidates": names}
    if op == "compare_candidates":
        if __package__:
            from .incremental import compare_candidates
        else:
            from incremental import compare_candidates
        if not info(request["source"])["readonly"] or not info(request["actual"])["readonly"]:
            raise ValueError("file comparison requires read-only snapshots")
        return compare_candidates(target(request["source"]), target(request["actual"]),
                                  request["candidates"], request["scope"])
    if op == "delete":
        if target(request["name"]).exists():
            command("btrfs", "subvolume", "delete", target(request["name"]))
        return {"deleted": True}
    if op == "verify":
        selected = target(request["source"])
        if request["source"] == "bases/C0":
            expected = json.loads((STORAGE / "expected-C0.json").read_text())
            if hashlib.sha256(encoded(expected)).hexdigest() != request["oracle_sha256"]:
                raise ValueError("C0 verification oracle differs from its saved checksum")
        else:
            expected = inventory(selected, request["scope"])
        actual = inventory(
            target(request["workspace"]), request["scope"], "." in expected["entries"]
        )
        result = compare(expected, actual)
        result["paths"] = len(actual["entries"])
        if not result["equal"]:
            raise ValueError(f"restored filesystem differs: {result}")
        return result
    if op == "usage":
        image = (STORAGE / "pool.img").stat()
        return {
            "image_capacity_bytes": image.st_size,
            "image_allocated_bytes": image.st_blocks * 512,
            "filesystem_usage": command("btrfs", "filesystem", "usage", "-b", POOL),
        }
    raise ValueError("unknown Btrfs worker operation")


if __name__ == "__main__":
    started = time.monotonic()
    try:
        value = operation(read_request(sys.argv[1:]))
        print(json.dumps({"ok": True, "result": value, "seconds": time.monotonic() - started}))
    except Exception as error:
        print(json.dumps({"ok": False, "type": type(error).__name__, "error": str(error)}))
        sys.exit(1)
