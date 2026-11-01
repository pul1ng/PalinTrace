from types import SimpleNamespace

from palinreplay.core.interfaces import Payload
from palinreplay.core.models import Operation, ToolGroup
from palinreplay.replay.history import supply_history
from palinreplay.storage.store import RecordStore
from tests.helpers import recorder_smoke as fake


def test_local_answer_through_model_port_has_new_identity(tmp_path):
    with RecordStore.create(tmp_path / "record") as store:
        agent, backend = fake.FakeAgent(), fake.FakeStateBackend()
        rec = fake.recorder(store, agent, backend)
        rec.start()
        step = rec.begin_step()
        answer = [{"role": "assistant", "content": "historical",
                   "extra": {"actions": [{"command": "saved", "tool_call_id": "t"}]}}]
        historical = SimpleNamespace(model=SimpleNamespace(
            messages=answer, source={"execution_id": "original", "operation_id": "old"}),
            tool_ids=("t",))

        def adopt(deliveries, messages):
            for call, offered in deliveries:
                start = len(agent.messages)
                call.deliver(offered, encode=Payload.json, receive=agent.messages.extend)
                call.accept({"start": start, "stop": len(agent.messages),
                             "messages": agent.messages[start:]}, encode=Payload.json)

        call = supply_history(step, historical, agent.messages, adopt)
        assert agent.messages[-1] == answer[0]
        assert call.execution_id != "original" and call.operation.operation_id != "old"
        assert store.list(Operation)[0].name == "replay.history"
        assert store.list(ToolGroup)[0].tool_call_ids == ("t",)
        rec.end_incomplete("only exercising historical answer supply")
        rec.close()
