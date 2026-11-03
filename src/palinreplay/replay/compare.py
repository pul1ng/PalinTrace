"""Exact tool comparison: bytes and definitive status, with timing kept separate."""

import hashlib


def compare_tool(historical, actual):
    expected_code = historical.details["backend"]["decoded"]["returncode"]
    definite = type(actual.exit_code) is int and isinstance(actual.output, bytes)
    same_output = definite and historical.raw == actual.output
    same_code = definite and expected_code == actual.exit_code
    return {
        "policy": "tool-exit-and-raw-bytes-v1",
        "status": "match" if same_output and same_code else "difference" if definite else "unknown",
        "equal": bool(same_output and same_code), "definitive": definite,
        "exit_code_equal": bool(same_code), "output_equal": bool(same_output),
        "source_exit_code": expected_code, "actual_exit_code": actual.exit_code,
        "source_output_bytes": len(historical.raw),
        "actual_output_bytes": len(actual.output) if definite else None,
        "source_output_sha256": hashlib.sha256(historical.raw).hexdigest(),
        "actual_output_sha256": hashlib.sha256(actual.output).hexdigest() if definite else None,
        "timing": {"source": historical.details.get("timing"),
                   "actual_duration_sec": actual.duration_sec},
        "source_execution_id": historical.source["execution_id"],
    }


def is_mtime_only_difference(files):
    """Allow only fully evidenced regular-file mtime changes, retaining the raw diff."""
    differences, paths = files.get("differences"), files.get("different_paths")
    if (files.get("equal") is not False or files.get("hardlinks_equal") is not True
            or files.get("hardlink_paths") != []
            or not isinstance(differences, list) or not differences
            or not isinstance(paths, list) or not all(isinstance(p, str) for p in paths)
            or len(differences) != files.get("different_path_count")
            or len(paths) != len(differences)):
        return False
    required = {"type", "mode", "uid", "gid", "size", "sha256", "mtime_sec"}
    seen = set()
    for difference in differences:
        if not isinstance(difference, dict):
            return False
        path = difference.get("path")
        source, actual = difference.get("source"), difference.get("actual")
        if (not isinstance(path, str) or path in seen
                or not isinstance(source, dict) or not isinstance(actual, dict)
                or not required <= source.keys() or source.keys() != actual.keys()
                or source["type"] != "file" or actual["type"] != "file"
                or type(source["mtime_sec"]) is not int or type(actual["mtime_sec"]) is not int
                or source["mtime_sec"] == actual["mtime_sec"]
                or any(source[key] != actual[key] for key in source if key != "mtime_sec")):
            return False
        seen.add(path)
    return seen == set(paths)
