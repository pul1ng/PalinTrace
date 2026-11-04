"""Acceptance oracle for a generated overlap-services manifest.

The expected A/B/C intervals in this script come from the task document and are
used only for acceptance, never by the preprocessing implementation.
"""

import argparse
import json
from pathlib import Path

from palinreplay.preprocess.manifest import SCHEMA_VERSION, load_manifest, select_services
from palinreplay.storage.io import write_json

EXPECTED = {"A": (5, 25), "B": (19, 30), "C": (35, 38)}
MATRIX = {
    0: [],
    19: ["A", "B"],
    25: ["B"],
    33: [],
    35: ["C"],
}


def main(manifest_path, output):
    manifest_path, output = manifest_path.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = load_manifest(manifest_path)
    summary = {"status": "failed", "manifest": str(manifest_path)}
    write_json(output / "acceptance.json", summary)
    try:
        assert manifest.source.start_index == 0 and manifest.source.end_index == 40
        intervals = {service.name: (service.start_inclusive, service.end_exclusive)
                     for service in manifest.services}
        assert len(manifest.services) == 3
        assert intervals == EXPECTED, intervals
        assert manifest.unknowns == (), manifest.unknowns
        for service in manifest.services:
            assert service.launch is not None, service.name
            assert f"/{service.name}/lifecycle.py" in service.launch.start_command
            if manifest.schema_version == SCHEMA_VERSION:
                assert service.launch.readiness_command is None
            else:
                assert f"/{service.name}/request.py" in service.launch.readiness_command
            assert service.launch.cwd == "/testbed"
            assert service.launch.timeout_sec == 30.0
        selections = {
            str(node): [item.name for item in select_services(manifest, node)]
            for node in MATRIX
        }
        assert selections == {str(node): names for node, names in MATRIX.items()}, selections
        summary.update(status="passed", intervals=intervals, selections=selections,
                       unknowns=0)
    except BaseException as error:
        summary.update(error_type=type(error).__name__, error=str(error))
        raise
    finally:
        write_json(output / "acceptance.json", summary, replace=True)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    main(args.manifest, args.output)
