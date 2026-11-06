"""Compare flat and shared payload storage without models, network, or containers.

Run with ``uv run python scripts/benchmarks/record_payloads.py --output outputs/bench``.
An optional ``--record`` reads a v1 capture through read-only SQLite and verified
artifact bytes. The copied catalog is only an accounting fixture: this benchmark
does not restore external rootfs pools or assert replay/resume validity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

from palinreplay.core.interfaces import Payload
from palinreplay.core.models import ArtifactRef
from palinreplay.core.serialization import json_bytes
from palinreplay.diagnostics.metrics import measure
from palinreplay.storage.store import RecordStore

PAYLOAD_ROLES = {
    "run.config", "node.context", "node.input_position", "execution.request",
    "execution.context", "event.payload", "event.details", "callevidence.payload",
    "runend.details", "failure.details",
}


def references(value, role):
    """Yield role/reference pairs without interpreting arbitrary payload content."""
    if isinstance(value, dict):
        if set(value) == {"sha256", "size", "media_type"}:
            yield role, ArtifactRef.model_validate(value)
        else:
            for key, child in value.items():
                yield from references(child, f"{role}.{key}")
    elif isinstance(value, list):
        for child in value:
            yield from references(child, f"{role}[]")


def artifact_path(root, digest):
    return root / "artifacts" / digest[:2] / digest[2:]


def file_totals(paths):
    paths = list(paths)
    stats = [path.stat() for path in paths]
    return {
        "file_count": len(paths),
        "apparent_bytes": sum(value.st_size for value in stats),
        "allocated_bytes": sum(value.st_blocks * 512 for value in stats),
    }


def tree_totals(root):
    return file_totals(path for path in root.rglob("*") if path.is_file())


def load_capture(root):
    """Read a captured v1 catalog and immutable bytes, never its state backend."""
    root = Path(root).resolve()
    with sqlite3.connect((root / "catalog.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if version != ("palinreplay.store.v1",):
            raise ValueError("captured baseline must be an unmodified v1 Record")
        rows = db.execute(
            "SELECT kind,id,run_id,ordinal,body,sha256 FROM records ORDER BY rowid"
        ).fetchall()
    payloads, bodies, objects = [], [], {}
    roles = defaultdict(dict)
    for kind, identifier, run_id, ordinal, body, digest in rows:
        if hashlib.sha256(body.encode()).hexdigest() != digest:
            raise ValueError("source catalog body hash mismatch")
        value = json.loads(body)
        bodies.append((kind, identifier, run_id, ordinal, value))
        for role, ref in references(value, kind):
            data = objects.get(ref.sha256)
            if data is None:
                data = artifact_path(root, ref.sha256).read_bytes()
                if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.sha256:
                    raise ValueError("source artifact size/hash mismatch")
                objects[ref.sha256] = data
            roles[role][ref.sha256] = ref.size
            if role in PAYLOAD_ROLES:
                payloads.append((role, Payload(data, ref.media_type)))
    referenced = set(objects)
    artifact_files = [path for path in (root / "artifacts").rglob("*") if path.is_file()]
    unreferenced = [
        path for path in artifact_files
        if path.parent.name + path.name not in referenced and ".tmp" not in path.parts
    ]
    baseline = {
        "schema_version": version[0],
        "record_counts": dict(Counter(row[0] for row in rows)),
        "record_root": tree_totals(root),
        "artifact_tree": file_totals(artifact_files),
        "catalog": file_totals([root / "catalog.sqlite"]),
        "catalog_referenced_artifacts": file_totals(
            artifact_path(root, digest) for digest in referenced
        ),
        "unreferenced_artifacts": file_totals(unreferenced),
        "temporary_files": tree_totals(root / "artifacts" / ".tmp"),
        "role_distinct_bytes": {
            role: {"file_count": len(values), "apparent_bytes": sum(values.values())}
            for role, values in sorted(roles.items())
        },
        "external_rootfs_pool": "excluded; not read or changed",
    }
    return {
        "payloads": payloads, "bodies": bodies, "objects": objects,
        "ancillary": [path for path in root.iterdir()
                      if path.is_file() and path.name != "catalog.sqlite"],
        "baseline": baseline,
    }


def synthetic_payloads(steps):
    """Canonical growing contexts, request wrappers, adoptions and final context."""
    messages = [
        {"role": "system", "content": "Inspect the workspace and explain changes. " * 48},
        {"role": "user", "content": "Complete the deterministic local fixture."},
    ]
    values = [("run.config", Payload.json({"agent": "benchmark", "model": "offline"}))]

    def context(index):
        return {"messages": messages, "counters": {"steps": index}, "config": {"seed": 0}}

    values.append(("node.context", Payload.json(context(0))))
    values.append(("node.input_position", Payload.json({"accepted": 0})))
    for index in range(steps):
        values.extend([
            ("execution.context", Payload.json(messages)),
            ("execution.request", Payload.json({
                "messages": messages, "model": "offline", "temperature": 0,
                "tools": [{"type": "function", "function": {"name": "inspect"}}],
            })),
        ])
        assistant = {"role": "assistant", "content": f"Step {index}: " + "reasoned action; " * 128}
        tool = {"role": "tool", "tool_call_id": f"call-{index}",
                "content": f"Result {index}: " + "observed fixture output\n" * 256}
        values.extend([
            ("event.payload", Payload.json(assistant)),
            ("event.payload", Payload.json(tool)),
            ("event.details", Payload.json({"decoded_output": tool["content"]})),
        ])
        messages.extend([assistant, tool])
        values.extend([
            ("event.payload", Payload.json({"adopted": messages[-2:]})),
            ("node.context", Payload.json(context(index + 1))),
            ("node.input_position", Payload.json({"accepted": 2 * (index + 1)})),
        ])
    values.append(("runend.details", Payload.json({
        "reason": "complete", "final_control_context": context(steps),
        "final_input_position": {"accepted": 2 * steps},
    })))
    return values


def rewrite_catalog(root, capture, replacements):
    """Copy fact relationships into a disposable accounting fixture."""
    def replace(value, role):
        if isinstance(value, dict):
            if set(value) == {"sha256", "size", "media_type"}:
                if role in PAYLOAD_ROLES:
                    key = (value["sha256"], value["media_type"])
                    return replacements[key].model_dump(mode="json")
                return value
            return {key: replace(child, f"{role}.{key}") for key, child in value.items()}
        if isinstance(value, list):
            return [replace(child, f"{role}[]") for child in value]
        return value

    rows = []
    for kind, identifier, run_id, ordinal, value in capture["bodies"]:
        body = json_bytes(replace(value, kind))
        rows.append((kind, identifier, run_id, ordinal, body.decode(),
                     hashlib.sha256(body).hexdigest()))
    with sqlite3.connect(root / "catalog.sqlite") as db:
        db.executemany("INSERT INTO records VALUES (?,?,?,?,?,?)", rows)
    for source in capture["ancillary"]:
        shutil.copyfile(source, root / source.name)


def run_sample(root, layout, payloads, capture=None):
    replacements, refs, closure = {}, [], {}
    with RecordStore.create(root, payload_layout=layout) as store:
        started = time.perf_counter()
        with measure() as save_metrics:
            for _role, payload in payloads:
                ref = store.put_payload(payload)
                refs.append(ref)
                replacements[(hashlib.sha256(payload.data).hexdigest(), payload.media_type)] = ref
        save_sec = time.perf_counter() - started
        started = time.perf_counter()
        with measure() as read_metrics:
            for (_role, payload), ref in zip(payloads, refs, strict=True):
                if store.read_payload(ref) != payload:
                    raise AssertionError("payload bytes or media type changed")
        read_sec = time.perf_counter() - started
        for ref in refs:
            for child in store.iter_payload_artifacts(ref):
                closure[child.sha256] = child
        if capture:
            for kind, _identifier, _run_id, _ordinal, body in capture["bodies"]:
                for role, ref in references(body, kind):
                    if role not in PAYLOAD_ROLES:
                        store.put_bytes(capture["objects"][ref.sha256], media_type=ref.media_type)
                        closure[ref.sha256] = ref
    if capture:
        rewrite_catalog(root, capture, replacements)
    artifacts = [path for path in (root / "artifacts").rglob("*") if path.is_file()]
    unreferenced = [path for path in artifacts
                    if path.parent.name + path.name not in closure and ".tmp" not in path.parts]
    return {
        "layout": layout,
        "payload_count": len(payloads),
        "logical_payload_bytes": sum(len(payload.data) for _role, payload in payloads),
        "distinct_logical_payload_bytes": sum({
            hashlib.sha256(payload.data).hexdigest(): len(payload.data)
            for _role, payload in payloads
        }.values()),
        "save_sec": save_sec,
        "read_sec": read_sec,
        "save_io": save_metrics.snapshot(),
        "read_io": read_metrics.snapshot(),
        "retained_artifacts": file_totals(artifacts),
        "referenced_artifact_closure": file_totals(
            artifact_path(root, digest) for digest in closure
        ),
        "unreferenced_artifacts": file_totals(unreferenced),
        "temporary_files_after_save": tree_totals(root / "artifacts" / ".tmp"),
        "catalog": file_totals([root / "catalog.sqlite"]),
        "record_root": tree_totals(root),
        "catalog_kind": "captured fact references remapped" if capture else "empty corpus store",
        "roundtrip": "exact bytes and media type for every input occurrence",
    }


def median_tree(values):
    first = values[0]
    if isinstance(first, dict):
        return {key: median_tree([value[key] for value in values]) for key in first}
    if isinstance(first, (float, int)):
        return statistics.median(values)
    return first


def paired_case(root, payloads, repeats=3, capture=None):
    trials = []
    for index in range(repeats):
        # Alternate order to reduce systematic warm-cache/order bias.
        order = ("flat", "shared") if index % 2 == 0 else ("shared", "flat")
        pair = {}
        for layout in order:
            pair[layout] = run_sample(root / f"pair-{index + 1}-{layout}", layout, payloads, capture)
            sample = pair[layout]
            print(f"{root.name} pair {index + 1} {layout}: "
                  f"save={sample['save_sec']:.3f}s read={sample['read_sec']:.3f}s "
                  f"artifacts={sample['retained_artifacts']['apparent_bytes']} bytes", flush=True)
        trials.append(pair)
    medians = {layout: median_tree([pair[layout] for pair in trials])
               for layout in ("flat", "shared")}
    return {"trials": trials, "median": medians, "paired_ratios": {
        key: statistics.median(
            pair["shared"][key] / pair["flat"][key] for pair in trials
        ) for key in ("save_sec", "read_sec")
    }}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, help="Optional read-only v1 Record root")
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    parser.add_argument("--steps", nargs="+", type=int, default=[2, 40, 100])
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1 or any(step < 1 for step in args.steps):
        parser.error("steps and repeats must be positive")
    args.output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": "palinreplay.payload-benchmark.v1",
        "method": {
            "repeats": args.repeats, "aggregate": "median of paired independent stores",
            "order": "alternating flat/shared", "cache": "OS cache not flushed",
            "timing": "payload save/read only; catalog rewrite and closure walk excluded",
            "io": "instrumented logical traffic, not device writes",
            "allocation": "sum(st_blocks * 512), including filesystem block rounding",
            "retained": "actual on-disk files after save; temporary/write traffic separate",
            "scope": "Record directory only; no external rootfs pools, model or container calls",
            "flat": "explicit flat layout under the same code as shared",
        },
        "cases": {},
    }
    if args.record:
        capture = load_capture(args.record)
        report["source_inventory"] = capture["baseline"]
        report["cases"]["captured"] = paired_case(
            args.output / "captured", capture["payloads"], args.repeats, capture
        )
    for steps in args.steps:
        name = f"synthetic-{steps}"
        report["cases"][name] = paired_case(
            args.output / name, synthetic_payloads(steps), args.repeats
        )
    destination = args.output / "report.json"
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(destination)


if __name__ == "__main__":
    main()
