import json

import pytest

from palinreplay.core.errors import RecordUsageError
from palinreplay.preprocess.manifest import (
    SCHEMA_VERSION,
    EvidenceRef,
    ManifestEvidence,
    ManifestLaunch,
    ManifestService,
    ManifestSource,
    ManifestUnknown,
    ServiceManifest,
    select_services,
)


def launch(name):
    return ManifestLaunch(
        start_command=f"start {name}", readiness_command=f"probe {name}",
        cwd="/testbed", env={}, timeout_sec=30,
    )


def service(name, start, end, *, with_launch=True):
    return ManifestService(
        name=name, start_inclusive=start, end_exclusive=end,
        launch=launch(name) if with_launch else None,
        evidence=ManifestEvidence(
            start=EvidenceRef(index=start, execution_id=f"{name}-start"),
            probe=EvidenceRef(index=start, execution_id=f"{name}-probe"),
            stop=EvidenceRef(index=end, execution_id=f"{name}-stop") if end <= 40 else None,
        ),
    )


def manifest(*services, unknown=()):
    return ServiceManifest(
        schema_version=SCHEMA_VERSION,
        source=ManifestSource(
            run_id="run", start_index=0, end_index=40,
            digest_scheme="sha256-canonical-selection-json-v1", selection_sha256="0" * 64,
        ),
        services=services,
        unknowns=unknown,
    )


@pytest.mark.parametrize(
    "node,names",
    [
        (0, []), (5, ["A"]), (18, ["A"]), (19, ["A", "B"]), (24, ["A", "B"]),
        (25, ["B"]), (29, ["B"]), (30, []), (33, []), (35, ["C"]), (37, ["C"]),
        (38, []), (40, []),
    ],
)
def test_same_manifest_selects_expected_services(node, names):
    value = manifest(service("A", 5, 25), service("B", 19, 30), service("C", 35, 38))
    assert [item.name for item in select_services(value, node)] == names


def test_unknown_or_missing_launch_blocks_selection():
    value = manifest(
        service("A", 5, 25, with_launch=False),
        unknown=(ManifestUnknown(
            name="B", start_inclusive=10, end_exclusive=20, kind="state",
            reason="state unproven",
        ),),
    )
    with pytest.raises(RecordUsageError, match=r"no verified launch.*A"):
        select_services(value, 6)
    with pytest.raises(RecordUsageError, match="state unproven"):
        select_services(value, 12)
    with pytest.raises(RecordUsageError, match="outside"):
        select_services(value, 41)


def test_manifest_rejects_overlap_out_of_range_and_empty_intervals():
    with pytest.raises(ValueError, match="overlapping"):
        manifest(service("A", 5, 25), service("A", 20, 30))
    with pytest.raises(ValueError, match="outside"):
        manifest(service("A", 5, 42))
    with pytest.raises(ValueError, match="nonempty"):
        ManifestService(
            name="A", start_inclusive=5, end_exclusive=5, launch=launch("A"),
            evidence=ManifestEvidence(
                start=EvidenceRef(index=5, execution_id="s"),
                probe=EvidenceRef(index=5, execution_id="p"),
            ),
        )


def test_manifest_round_trip_and_unknown_shape():
    value = manifest(
        service("A", 5, 25),
        unknown=(ManifestUnknown(
            name="A", start_inclusive=25, end_exclusive=40, kind="state",
            reason="tail not observed",
        ),),
    )
    document = value.model_dump(mode="json")
    assert document["schema_version"] == SCHEMA_VERSION
    assert json.loads(value.model_dump_json()) == document
