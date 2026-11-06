from collections import Counter
from dataclasses import replace

import pytest

import palinreplay.storage.payloads as payload_storage
from palinreplay.core.interfaces import BoundaryAdapter
from palinreplay.core.models import Execution, Node, RunEnd
from palinreplay.record.calls import RecordingError
from palinreplay.storage.payloads import ROOT_MEDIA
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake


def test_complete_node_commit_reuses_precheck_expansion(tmp_path, monkeypatch):
    agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
    agent.messages[0]["content"] = "initial message " * 2000
    with RecordStore.create(tmp_path / "record") as store:
        recorder = fake.recorder(store, agent, backend)
        counts = Counter()
        original = payload_storage._Reader.read

        def read(reader):
            if store._payloads._expanded.get() is not None:
                counts[reader.root] += 1
            return original(reader)

        monkeypatch.setattr(payload_storage._Reader, "read", read)
        recorder.start()
        assert counts and max(counts.values()) == 1
        assert store._payloads._expanded.get() is None
        fake.answer(recorder, agent, ())
        execution = store.list(Execution)[0]
        assert execution.request.media_type == ROOT_MEDIA
        counts.clear()
        node = recorder.commit_step()
        assert node.index == 1
        assert counts[execution.request] == 1
        assert max(counts.values()) == 1
        assert store._payloads._expanded.get() is None
        recorder.close()


def test_corrupted_cached_request_prevents_node_publication(tmp_path):
    agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
    agent.messages[0]["content"] = "initial message " * 2000
    with RecordStore.create(tmp_path / "record") as store:
        recorder = fake.recorder(store, agent, backend)
        initial = recorder.start()
        calls = []
        adapter = fake.model_adapter()

        def execute(request):
            calls.append(request)
            return adapter.execute(request)

        step = recorder.begin_step()
        model = step.model(replace(adapter, execute=execute),
                           {"messages": agent.messages, "tool_calls": ()})
        reply = model.invoke()
        fake.adopt(agent, model, reply)
        step.declare_tools(model, ())

        def capture(record_store, boundary, previous):
            record_store._verify_refs([model.execution])
            ref = model.execution.request
            assert ref.media_type == ROOT_MEDIA
            assert record_store._payloads._expanded.get()
            path = record_store.root / "artifacts" / ref.sha256[:2] / ref.sha256[2:]
            data = path.read_bytes()
            path.chmod(0o600)
            path.write_bytes(data[:-2] + b"!\n")
            path.chmod(0o400)
            return backend.capture(record_store, boundary, previous)

        recorder.boundary = BoundaryAdapter(agent.export, capture)
        with pytest.raises(RecordingError):
            recorder.commit_step()
        assert store._payloads._expanded.get() is None
        assert len(calls) == len(store.list(Execution)) == 1
        assert store.list(Node) == (initial,)
        assert store.get_node(initial.node_id) == initial
        assert store.list(RunEnd)[0].status == "failed"
        recorder.close()
