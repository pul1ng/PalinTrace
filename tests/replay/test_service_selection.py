from types import SimpleNamespace

import pytest

from palinreplay.core.errors import RecordUsageError
from palinreplay.preprocess.manifest import EvidenceRef, ManifestEvidence, ManifestSource
from palinreplay.record.selection import pin_source_selection
from palinreplay.restoration.service_config import ServiceInput, resolve_services
from palinreplay.resume.services import resolve_resume_services
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake
from tests.preprocess.test_manifest import manifest, service


@pytest.fixture(scope="module")
def overlap_source(tmp_path_factory):
    root = tmp_path_factory.mktemp("service-selection") / "record"
    agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
    with RecordStore.create(root) as store:
        with fake.recorder(store, agent, backend) as rec:
            for index in range(1, 41):
                identity = f"tool-{index}"
                step = fake.answer(rec, agent, (identity,))
                call = step.tool(fake.tool_adapter(backend), {"action": "read"}, tool_call_id=identity)
                fake.adopt(agent, call, {"role": "tool", "tool_call_id": identity, **call.invoke()})
                rec.commit_step()
            rec.finish()
        full = pin_source_selection(store, "C0", "C40")
    nodes = {node.index: node for node in full.nodes}

    def evidence(index):
        return EvidenceRef(index=index, execution_id=nodes[index].tool_execution_ids[0])

    services = tuple(service(name, first, last).model_copy(update={
        "evidence": ManifestEvidence(start=evidence(first), probe=evidence(first), stop=evidence(last)),
    }) for name, first, last in (("A", 5, 25), ("B", 19, 30), ("C", 35, 38)))
    provenance = full.provenance()
    value = manifest(*services).model_copy(update={"source": ManifestSource(
        run_id=full.run.run_id, start_index=0, end_index=40,
        digest_scheme=provenance["digest_scheme"], selection_sha256=provenance["selection_sha256"],
    )})
    return root, nodes, value


@pytest.mark.parametrize("index,names", [
    (0, []), (5, ["A"]), (19, ["A", "B"]), (20, ["A", "B"]),
    (25, ["B"]), (30, []), (35, ["C"]), (38, []),
])
def test_shared_and_resume_resolvers_select_the_same_half_open_intervals(overlap_source, index, names):
    root, nodes, value = overlap_source
    configured = ServiceInput(None, value, root.parent / "services.json")
    with RecordStore.open(root, read_only=True) as store:
        selected = resolve_services(configured, store, nodes[index], end_index=index)
    assert [item.name for item in selected.services] == names
    start = SimpleNamespace(source_store=root, node=nodes[index])
    assert [item.name for item in resolve_resume_services(start, configured).services] == names


def test_resume_start_can_be_valid_when_replay_end_is_outside_analysis(overlap_source):
    root, nodes, value = overlap_source
    with RecordStore.open(root, read_only=True) as store:
        partial = pin_source_selection(store, "C0", "C20").provenance()
        value = value.model_copy(update={"services": (), "source": value.source.model_copy(update={
            "end_index": 20, "selection_sha256": partial["selection_sha256"],
        })})
        configured = ServiceInput(None, value, root.parent / "services.json")
        with pytest.raises(RecordUsageError, match="outside the manifest"):
            resolve_services(configured, store, nodes[20], end_index=22)
    start = SimpleNamespace(source_store=root, node=nodes[20])
    assert resolve_resume_services(start, configured).services == ()


@pytest.mark.parametrize("field", ["start", "probe", "stop"])
def test_unselected_service_evidence_is_still_validated(overlap_source, field):
    root, nodes, value = overlap_source
    unselected = value.services[-1]  # C is inactive at C20.
    ref = getattr(unselected.evidence, field).model_copy(update={"execution_id": "missing"})
    evidence = unselected.evidence.model_copy(update={field: ref})
    value = value.model_copy(update={"services": (*value.services[:-1],
                                                  unselected.model_copy(update={"evidence": evidence}))})
    with RecordStore.open(root, read_only=True) as store:
        with pytest.raises(RecordUsageError, match="manifest evidence"):
            resolve_services(ServiceInput(None, value, root.parent / "services.json"), store, nodes[20])
