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


def test_c19_compound_command_extracts_only_b_service_fragments():
    call = make_call(
        "ff86-19",
        "/srv/B/lifecycle.py start && /srv/A/request.py && /srv/B/request.py",
    )
    life, service = lifecycle(
        "B", call, "/srv/B/lifecycle.py start", call, "/srv/B/request.py",
        ("program:/srv/B/lifecycle.py",),
    )
    other = ServiceEvidence(
        name="A", aliases=("A",), identity_keys=("program:/srv/A/lifecycle.py",),
        items=(), identity_unknown=False, unknowns=(),
    )
    recipe = build_recipes(timeline(call), (life,), (service, other), timeout_sec=30)[0]
    assert recipe.launch.start_command == "/srv/B/lifecycle.py start"
    assert recipe.launch.readiness_command == "/srv/B/request.py"
    assert recipe.launch.cwd == "/testbed" and recipe.launch.env == {}
    assert recipe.launch.timeout_sec == 30
    assert recipe.unknowns == ()


def test_whole_compound_or_embedded_fragment_is_not_an_independent_recipe():
    call = make_call(
        "ff86-19",
        "/srv/B/lifecycle.py start && /srv/B/request.py",
    )
    for quote in (
        "/srv/B/lifecycle.py start && /srv/B/request.py",
        "lifecycle.py start",
    ):
        life, service = lifecycle(
            "B", call, quote, call, "/srv/B/request.py", ("program:/srv/B/lifecycle.py",)
        )
        recipe = build_recipes(timeline(call), (life,), (service,), timeout_sec=30)[0]
        assert recipe.launch is None
        assert "complete top-level" in recipe.unknowns[0].reason


def test_other_service_references_and_unsafe_readiness_are_rejected():
    call = make_call("ff86-19", "/srv/B/lifecycle.py start && /srv/A/request.py")
    life, service = lifecycle(
        "B", call, "/srv/B/lifecycle.py start", call, "/srv/A/request.py",
        ("program:/srv/B/lifecycle.py",),
    )
    other = ServiceEvidence(
        name="A", aliases=("A",), identity_keys=("program:/srv/A/request.py",),
        items=(), identity_unknown=False, unknowns=(),
    )
    recipe = build_recipes(timeline(call), (life,), (service, other), timeout_sec=30)[0]
    assert recipe.launch is None and "another service" in recipe.unknowns[0].reason

    same = make_call("ff86-19", "/srv/B/lifecycle.py start")
    life, service = lifecycle(
        "B", same, "/srv/B/lifecycle.py start", same, "/srv/B/lifecycle.py start",
        ("program:/srv/B/lifecycle.py",),
    )
    recipe = build_recipes(timeline(same), (life,), (service,), timeout_sec=30)[0]
    assert recipe.launch is None and "not safe to repeat" in recipe.unknowns[0].reason


def test_cwd_dependencies_and_environment_must_be_complete():
    start = make_call("e-5", "/srv/B/lifecycle.py start", cwd="/testbed")
    probe = make_call("e-6", "/srv/B/request.py", cwd="/other")
    life, service = lifecycle(
        "B", start, "/srv/B/lifecycle.py start", probe, "/srv/B/request.py",
        ("program:/srv/B/lifecycle.py",),
    )
    recipe = build_recipes(timeline(start, probe), (life,), (service,), timeout_sec=30)[0]
    assert recipe.launch is None and "working directories" in recipe.unknowns[0].reason

    start = make_call(
        "e-5", "/srv/B/lifecycle.py start", env={"PORT": "9999"}, cwd="/testbed"
    )
    probe = make_call("e-6", "/srv/B/request.py $PORT", env={"PORT": "9999"}, cwd="/testbed")
    life, service = lifecycle(
        "B", start, "/srv/B/lifecycle.py start", probe, "/srv/B/request.py $PORT",
        ("program:/srv/B/lifecycle.py",),
    )
    recipe = build_recipes(timeline(start, probe), (life,), (service,), timeout_sec=30)[0]
    assert recipe.launch.env == {"PORT": "9999"}

    missing = make_call("e-5", "/srv/B/lifecycle.py start", env={})
    missing_probe = make_call("e-6", "/srv/B/request.py $PORT", env={}, cwd="/testbed")
    life, service = lifecycle(
        "B", missing, "/srv/B/lifecycle.py start", missing_probe,
        "/srv/B/request.py $PORT", ("program:/srv/B/lifecycle.py",),
    )
    recipe = build_recipes(
        timeline(missing, missing_probe), (life,), (service,), timeout_sec=30
    )[0]
    assert recipe.launch is None and "environment" in recipe.unknowns[0].reason


def test_unresolved_dependencies_and_unsupported_shell_remain_unknown():
    call = make_call("e-5", "/srv/B/lifecycle.py start")
    life, service = lifecycle(
        "B", call, "/srv/B/lifecycle.py start", call, "/srv/B/lifecycle.py start",
        ("program:/srv/B/lifecycle.py",), start_deps=("database",),
    )
    recipe = build_recipes(timeline(call), (life,), (service,), timeout_sec=30)[0]
    assert recipe.launch is None and "dependencies" in recipe.unknowns[0].reason

    unsupported = make_call("e-5", "echo $(/srv/B/start)")
    life, service = lifecycle(
        "B", unsupported, "echo $(/srv/B/start)", unsupported, "echo $(/srv/B/start)",
        ("program:/srv/B/start",),
    )
    recipe = build_recipes(timeline(unsupported), (life,), (service,), timeout_sec=30)[0]
    assert recipe.launch is None


def test_recipe_skips_unsafe_earlier_probe_and_uses_later_dedicated_one():
    start_call = make_call(
        "e-5", "/srv/B/lifecycle.py start && python -c 'check 18765'"
    )
    probe_call = make_call("e-6", "/srv/B/request.py")
    start = event(start_call, "/srv/B/lifecycle.py start", "start", output_quote="READY")
    checker = event(start_call, "python -c 'check 18765'", "probe", output_quote="READY")
    dedicated = event(probe_call, "/srv/B/request.py", "probe", output_quote="READY")
    interval = ServiceInterval(
        name="B", start_inclusive=5, end_exclusive=10,
        start_event=start, probe_event=checker, stop_event=None,
        last_live_event=dedicated, open_end=False, events=(start, checker, dedicated),
    )
    item = CandidateEvidence(
        candidate=Candidate(
            name="B", node_index=5, execution_id=start_call.execution_id,
            actions=("start", "probe"), candidate_state="up",
        ),
        identity=(), events=(start, checker), unknowns=(),
    )
    service = ServiceEvidence(
        name="B", aliases=("B",), identity_keys=("program:/srv/B/lifecycle.py",),
        items=(item,), identity_unknown=False, unknowns=(),
    )
    other = ServiceEvidence(
        name="A", aliases=("A",), identity_keys=("endpoint:18765",),
        items=(), identity_unknown=False, unknowns=(),
    )
    recipe = build_recipes(
        timeline(start_call, probe_call),
        (ServiceLifecycle(service, (interval,), ()),),
        (service, other),
        timeout_sec=30,
    )[0]
    assert recipe.launch.readiness_command == "/srv/B/request.py"
    assert recipe.probe_event is dedicated


def test_split_top_level_ignores_quoted_operators_but_preserves_pipelines():
    segments = split_top_level("python -c 'a && b' && grep x | wc -l; echo done")
    assert [segment.text for segment in segments] == [
        "python -c 'a && b'",
        "grep x | wc -l",
        "echo done",
    ]
    with pytest.raises(ValueError, match="command substitution"):
        split_top_level("echo $(date)")
    with pytest.raises(ValueError, match="subshell"):
        split_top_level("(cd / && run)")
