import json
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from examples import resume_multiturn_experiments as experiments
from examples import resume_multiturn_validation as validation
from palinreplay.agents.mini import load_native
from palinreplay.core.errors import RecordUsageError
from palinreplay.resume import decision
from palinreplay.storage.store import RecordStore
from tests.resume import test_decision

session = test_decision.session
llm_config = test_decision.llm_config


@pytest.mark.parametrize("relative_output", ["record/validation", "validation"])
def test_multistart_rejects_output_inside_source_before_writing(tmp_path, monkeypatch, relative_output):
    source = tmp_path / "source" / "record"
    with RecordStore.create(source):
        pass
    monkeypatch.setattr(experiments, "SOURCES", (replace(experiments.SOURCES[0], store=source),))
    output = source.parent / relative_output
    with pytest.raises(RecordUsageError, match="outside"):
        experiments.run_starts(output, tmp_path / "services.json")
    assert not output.exists()


@pytest.mark.parametrize("case", ["missing_prefix", "wrong_http_body", "before_http", "http_error"])
def test_real100_distinguishes_verification_errors_from_failed_requests(
    tmp_path, monkeypatch, session, llm_config, case,
):
    output = tmp_path / "live"
    session.executor._container.id = "test-container"
    session.start.node.model_dump = lambda **kwargs: {"index": 20}
    monkeypatch.setattr(experiments, "connect_docker", Mock())
    monkeypatch.setattr(experiments, "audit_source", lambda *args: nullcontext())
    monkeypatch.setattr(experiments, "select_resume_start", lambda *args, **kwargs: session.start)
    monkeypatch.setattr(validation.service_module, "start_service", lambda *args: None)

    @contextmanager
    def restore(*args, **kwargs):
        for name in ("A", "B"):
            validation.service_module.start_service(session.executor, SimpleNamespace(name=name), None)
        try:
            yield session
        finally:
            session.report.update(status="failed", cleanup_confirmed=True)

    monkeypatch.setattr(experiments, "restore_resume_start", restore)
    monkeypatch.setattr(experiments.restoration, "checked_report", lambda *args: session.report)
    if case == "missing_prefix":
        original = decision.prepare_branch_decision

        def drop_first_message(*args, **kwargs):
            prepared = original(*args, **kwargs)
            prepared.request["messages"] = prepared.request["messages"][1:]
            return prepared

        monkeypatch.setattr(decision, "prepare_branch_decision", drop_first_message)

    def provider(**request):
        if case == "before_http":
            raise RuntimeError("provider unavailable before HTTP")
        messages = request["messages"][1:] if case == "wrong_http_body" else request["messages"]
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401))) as client:
            client.post("https://model.invalid/v1/chat/completions",
                        json={"messages": messages}).raise_for_status()

    monkeypatch.setattr(load_native().litellm, "completion", provider)
    if case in ("missing_prefix", "wrong_http_body"):
        with pytest.raises(AssertionError):
            experiments.run_real100(output, tmp_path / "services.json", llm_config)
        report = json.loads((output / "acceptance.json").read_bytes())
        assert report["status"] == "failed" and report["full_requests_verified"] is False
    else:
        report = experiments.run_real100(output, tmp_path / "services.json", llm_config)
        assert report["status"] == "fault"
        assert report["full_requests_verified"] is (case == "http_error")
        assert report["model_requests"] == 1
        assert report["successful_decisions"] == report["actual_rounds"] == report["complete_nodes"] == 0
    assert report["http_requests"] == int(case in ("wrong_http_body", "http_error"))
