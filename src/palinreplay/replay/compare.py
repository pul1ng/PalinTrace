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
