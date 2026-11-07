from collections import Counter
from dataclasses import replace

import pytest

import palinreplay.storage.payloads as payloads
from palinreplay.core.interfaces import Payload
from palinreplay.core.models import Event, Node
from palinreplay.record.calls import RecordingError, deliver_batch
from palinreplay.storage.payloads import NODE_MEDIA, ROOT_MEDIA
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake


def setup(store):
    agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
    agent.messages[0]["content"] = "large inherited context " * 1500
    recorder = fake.recorder(store, agent, backend)
    recorder.start()
    return agent, recorder, recorder.begin_step()


@pytest.mark.parametrize("batch", [False, True])
def test_call_operations_expand_each_payload_once_and_release(tmp_path, monkeypatch, batch):
    with RecordStore.create(tmp_path / "record") as store:
        agent, recorder, step = setup(store)
        counts = Counter()
        original = payloads._Reader.read

        def read(reader):
            counts[reader.root] += 1
            return original(reader)

        monkeypatch.setattr(payloads._Reader, "read", read)

        def checked(operation):
            counts.clear()
            value = operation()
            assert counts and max(counts.values()) == 1
            assert store._payloads._expanded.get() is None
            return value

        call = checked(lambda: step.model(
            fake.model_adapter(), {"messages": agent.messages, "tool_calls": ()}
        ))
        assert call.execution.request.media_type == ROOT_MEDIA
        reply = checked(call.invoke)
        if batch:
            checked(lambda: deliver_batch(
                [(call, reply)], encode=Payload.json,
                receive=lambda values: [agent.receive(value) for value in values],
            ))
        else:
            checked(lambda: call.deliver(reply, encode=Payload.json, receive=agent.receive))
        checked(lambda: call.accept(agent.messages[-1], encode=Payload.json))
        step.declare_tools(call, ())
        assert recorder.commit_step().index == 1
        recorder.finish()
        recorder.close()
        assert store.check_integrity().ok


@pytest.mark.parametrize("phase", ["invoke", "handoff"])
def test_call_cache_rechecks_physical_children_after_callbacks(tmp_path, phase):
    with RecordStore.create(tmp_path / "record") as store:
        agent, recorder, step = setup(store)
        executed, received = [], []
        adapter = fake.model_adapter()
        child = None

        def corrupt():
            assert store._payloads._expanded.get()
            path = store.root / "artifacts" / child.sha256[:2] / child.sha256[2:]
            data = path.read_bytes()
            path.chmod(0o600)
            path.write_bytes(data[:-2] + b"!\n")

        def execute(request):
            executed.append(request)
            if phase == "invoke":
                corrupt()
            return adapter.execute(request)

        call = step.model(replace(adapter, execute=execute),
                          {"messages": agent.messages, "tool_calls": ()})
        child = next(ref for ref in store.iter_payload_artifacts(call.execution.request)
                     if ref.media_type == NODE_MEDIA)
        if phase == "invoke":
            with pytest.raises(RecordingError):
                call.invoke()
        else:
            reply = call.invoke()

            def receive(value):
                received.append(value)
                agent.receive(value)
                corrupt()

            with pytest.raises(RecordingError):
                call.deliver(reply, encode=Payload.json, receive=receive)
        assert store._payloads._expanded.get() is None
        assert len(executed) == 1 and len(received) == (phase == "handoff")
        assert len(store.list(Node)) == 1
        assert [event.kind for event in store.list(Event)] == (
            ["dispatched"] if phase == "invoke" else ["dispatched", "result"]
        )
        recorder.close()
