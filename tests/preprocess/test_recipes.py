from dataclasses import replace

import pytest

from palinreplay.core.models import ArtifactRef
from palinreplay.preprocess.candidates import Candidate
from palinreplay.preprocess.evidence import (
    CandidateEvidence,
    ServiceEvidence,
    VerifiedEvent,
    VerifiedSpan,
)
from palinreplay.preprocess.lifecycle import ServiceInterval, ServiceLifecycle
from palinreplay.preprocess.recipes import build_recipes, split_top_level
from palinreplay.preprocess.timeline import (
    Timeline,
    TimelineCall,
    TimelineRequest,
    TimelineResult,
    TimelineSource,
)


def make_call(execution, command, output="READY\n", cwd="/testbed", env=None):
    return TimelineCall(
        node_index=int(execution.rsplit("-", 1)[-1]) if execution[-1].isdigit() else 5,
        tool_ordinal=1,
        execution_id=execution,
        tool_name="mini.bash",
        request=TimelineRequest(
            command=command, interpreter=("bash", "-c"), cwd=cwd,
            env=env or {}, timeout_sec=60,
        ),
        result=TimelineResult(outcome="returned", exit_code=0, output_text=output),
        refs={
            "request": ArtifactRef(sha256="1" * 64, size=1),
            "output": ArtifactRef(sha256="2" * 64, size=1),
            "details": ArtifactRef(sha256="3" * 64, size=1),
        },
    )


def make_span(command, quote, output="READY\n"):
    if quote in command:
        start = command.index(quote)
        return VerifiedSpan(
            field="request.command", quote=quote, start=start, end=start + len(quote),
            artifact_sha256="1" * 64,
        )
    start = output.index(quote)
    return VerifiedSpan(
        field="result.output_text", quote=quote, start=start, end=start + len(quote),
        artifact_sha256="2" * 64,
    )


def event(call, quote, action, state="up", output_quote=None, executable=True, deps=()):
    return VerifiedEvent(
        node_index=call.node_index,
        tool_ordinal=1,
        execution_id=call.execution_id,
        action=action,
        observed_state=state,
        action_result="succeeded" if action != "probe" else "unattributed",
        request_span=make_span(call.request.command, quote) if quote else None,
        output_span=make_span(call.request.command, output_quote) if output_quote else None,
        executable=executable,
        dependencies=deps,
        reason="fixture",
    )


def lifecycle(
    name, start_call, start_quote, probe_call, probe_quote, identity_keys=(),
    start_deps=(), probe_deps=(),
):
    start = event(
        start_call, start_quote, "start", output_quote="READY", deps=start_deps
    )
    probe = event(
        probe_call, probe_quote, "probe", output_quote="READY", deps=probe_deps
    )
    interval = ServiceInterval(
        name=name,
        start_inclusive=start_call.node_index,
        end_exclusive=start_call.node_index + 5,
        start_event=start,
        probe_event=probe,
        stop_event=None,
        last_live_event=probe,
        open_end=False,
        events=(start, probe),
    )
    item = CandidateEvidence(
        candidate=Candidate(
            name=name, node_index=start_call.node_index, execution_id=start_call.execution_id,
            actions=("start",), candidate_state="up",
        ),
        identity=(),
        events=(start,),
        unknowns=(),
    )
    service = ServiceEvidence(
        name=name, aliases=(name,), identity_keys=identity_keys, items=(item,),
        identity_unknown=False, unknowns=(),
    )
    return ServiceLifecycle(service, (interval,), ()), service


def timeline(*calls):
    return Timeline(
        source=TimelineSource(
            run_id="run", start_index=0, end_index=40,
            digest_scheme="sha256-canonical-selection-json-v1", selection_sha256="0" * 64,
        ),
        calls=calls,
    )


def recipe_for(call, quote, **kwargs):
    life, service = lifecycle("B", call, quote, call, quote,
                              ("program:/srv/B/lifecycle.py",), **kwargs)
    return build_recipes(timeline(call), (life,), (service,), timeout_sec=30)[0]


def test_c19_extracts_only_original_start_and_never_generates_readiness():
    call = make_call("ff86-19", "/srv/B/lifecycle.py start && /srv/A/request.py && /srv/B/request.py")
    recipe = recipe_for(call, "/srv/B/lifecycle.py start")
    assert recipe.launch.start_command == "/srv/B/lifecycle.py start"
    assert recipe.launch.cwd == "/testbed" and recipe.launch.env == {}
    assert recipe.launch.timeout_sec == 30
    assert "readiness_command" not in recipe.launch.model_dump()


def test_background_start_preserves_redirections_and_needs_no_probe():
    call = make_call("e-1", "./start-A.sh >a.log 2>&1 &", output="")
    life, service = lifecycle("A", call, call.request.command, call, call.request.command)
    interval = life.intervals[0]
    interval = replace(interval, probe_event=None, events=(interval.start_event,))
    life = ServiceLifecycle(service, (interval,), ())
    recipe = build_recipes(timeline(call), (life,), (service,), timeout_sec=30)[0]
    assert recipe.launch.start_command == call.request.command
    assert "readiness_command" not in recipe.document()["launch"]


@pytest.mark.parametrize("quote", [
    "/srv/B/lifecycle.py start && /srv/B/request.py", "lifecycle.py start",
])
def test_whole_compound_or_embedded_fragment_fails_publication(quote):
    call = make_call("e-1", "/srv/B/lifecycle.py start && /srv/B/request.py")
    with pytest.raises(ValueError, match="complete top-level"):
        recipe_for(call, quote)


def test_start_referencing_another_service_is_rejected():
    call = make_call("e-1", "/srv/B/lifecycle.py start /srv/A/config")
    life, service = lifecycle("B", call, call.request.command, call, call.request.command)
    other = ServiceEvidence(name="A", aliases=("A",), identity_keys=("config:/srv/A/config",),
                            items=(), identity_unknown=False, unknowns=())
    with pytest.raises(ValueError, match="another service"):
        build_recipes(timeline(call), (life,), (service, other), timeout_sec=30)


def test_all_recorded_environment_overrides_are_kept_even_without_shell_expansion():
    call = make_call("e-1", "/srv/B/lifecycle.py start", env={"PORT": "9999", "FLAG": "v"})
    assert recipe_for(call, call.request.command).launch.env == {"PORT": "9999", "FLAG": "v"}
    missing = make_call("e-1", "/srv/B/lifecycle.py start $PORT")
    with pytest.raises(ValueError, match="environment"):
        recipe_for(missing, missing.request.command)


def test_only_start_dependencies_and_cwd_affect_recipe():
    call = make_call("e-1", "/srv/B/lifecycle.py start", cwd="/srv/B")
    with pytest.raises(ValueError, match="dependencies"):
        recipe_for(call, call.request.command, start_deps=("database",))
    probe = make_call("e-2", "/srv/B/request.py $MISSING", cwd="/other")
    life, service = lifecycle("B", call, call.request.command, probe, probe.request.command,
                              probe_deps=("unrelated",))
    recipe = build_recipes(timeline(call, probe), (life,), (service,), timeout_sec=30)[0]
    assert recipe.launch.cwd == "/srv/B"
    assert recipe.launch.env == {}


def test_unsupported_shell_start_cannot_be_published_as_a_null_launch():
    call = make_call("e-1", "echo $(/srv/B/start)")
    with pytest.raises(ValueError, match="top-level"):
        recipe_for(call, call.request.command)


def test_split_top_level_ignores_quoted_operators_and_keeps_background_launch():
    segments = split_top_level("python -c 'a && b' && grep x | wc -l; ./start-A.sh >a.log 2>&1 &")
    assert [segment.text for segment in segments] == [
        "python -c 'a && b'", "grep x | wc -l", "./start-A.sh >a.log 2>&1 &"]
    with pytest.raises(ValueError, match="command substitution"):
        split_top_level("echo $(date)")
    with pytest.raises(ValueError, match="subshell"):
        split_top_level("(cd / && run)")
