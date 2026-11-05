from types import SimpleNamespace

import pytest

from palinreplay.agents.mini import MINI_COMMIT, MINI_VERSION
from palinreplay.core.errors import IntegrityError, RecordUsageError
from palinreplay.resume.context import validate_resume_context


@pytest.fixture
def materials():
    config = {"agent": {}, "model": {}, "environment": {}}
    return SimpleNamespace(
        run=SimpleNamespace(agent_name="mini-swe-agent",
                            agent_version=f"{MINI_VERSION}+{MINI_COMMIT}"),
        node=SimpleNamespace(index=20, accepted_event_sequence=160), config=config,
        context={
            "messages": [{"role": "user", "content": "task"},
                         {"role": "tool", "tool_call_id": "service-tool-20"}],
            "config": config, "counters": {"n_calls": 20, "cost": 0,
                                          "consecutive_format_errors": 0},
            "extra_template_vars": {}, "template_vars": {}, "clock": {},
            "global_stats": {}, "model_accounting": {},
        },
        input_position={"next_message": 2, "adoptions": [
            {"start": 1, "stop": 2, "event_sequence": 160, "execution_id": "tool20"},
        ]},
    )


def test_boundary_keeps_history_and_does_not_consult_run_end(materials):
    prefix = validate_resume_context(materials)
    assert prefix.context is materials.context and prefix.counters["n_calls"] == 20


def test_explicit_exit_has_a_reason(materials):
    materials.context["messages"][-1] = {"role": "exit", "extra": {"exit_status": "Submitted"}}
    with pytest.raises(RecordUsageError, match=r"C20 is terminal: exit message 1 \(Submitted\)"):
        validate_resume_context(materials)


@pytest.mark.parametrize("field", ["agent_name", "agent_version"])
def test_pinned_mini_required(materials, field):
    setattr(materials.run, field, "unsupported")
    with pytest.raises(IntegrityError, match="pinned"):
        validate_resume_context(materials)


@pytest.mark.parametrize("field", ["messages", "config", "counters", "clock",
                                  "template_vars", "extra_template_vars", "global_stats",
                                  "model_accounting"])
def test_required_context_structure(materials, field):
    del materials.context[field]
    with pytest.raises(IntegrityError):
        validate_resume_context(materials)


@pytest.mark.parametrize("cursor", [True, -1, 1, 3, "2", None])
def test_cursor_must_match_entire_prefix(materials, cursor):
    materials.input_position["next_message"] = cursor
    with pytest.raises(IntegrityError, match="cursor"):
        validate_resume_context(materials)


@pytest.mark.parametrize("change", [{"stop": 3}, {"start": -1}, {"event_sequence": 161},
                                     {"execution_id": ""}, {"start": True}])
def test_adoption_cannot_cross_boundary(materials, change):
    materials.input_position["adoptions"][0].update(change)
    with pytest.raises(IntegrityError, match="adoption"):
        validate_resume_context(materials)
