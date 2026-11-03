"""End-to-end preprocessing orchestration and publishable report."""

from dataclasses import dataclass
from pathlib import Path

from palinreplay.llm import OpenAIChatClient
from palinreplay.preprocess.candidates import (
    CANDIDATE_PROMPT_VERSION,
    candidate_template,
    recognize_candidates,
)
from palinreplay.preprocess.chunks import TokenCounter, make_token_counter, plan_chunks
from palinreplay.preprocess.config import PreprocessConfig
from palinreplay.preprocess.evidence import (
    EVIDENCE_PROMPT_VERSION,
    EvidenceTemplate,
    analyze_evidence,
)
from palinreplay.preprocess.lifecycle import analyze_lifecycles
from palinreplay.preprocess.manifest import ServiceManifest, build_manifest, write_manifest
from palinreplay.preprocess.recipes import build_recipes
from palinreplay.preprocess.source import PinnedSource, pin_source
from palinreplay.preprocess.timeline import Timeline, read_timeline
from palinreplay.storage.io import outside_store, write_json
from palinreplay.storage.store import RecordStore

REPORT_SCHEMA = "palinreplay.preprocess-report.v1"


@dataclass(frozen=True)
class PreprocessResult:
    manifest: ServiceManifest
    report: dict
    output: Path


def analyze(
    source_path: str | Path,
    *,
    run_id: str | None,
    config: PreprocessConfig,
    client,
    counter: TokenCounter | None = None,
) -> tuple[ServiceManifest, dict, Timeline, PinnedSource]:
    """Read the Record once, then run candidate and evidence passes."""
    with RecordStore.open(source_path) as store, store.snapshot():
        source = pin_source(store, run_id)
        timeline = read_timeline(store, source)
    counter = counter or make_token_counter(config)
    settings = config.preprocess
    chunks = plan_chunks(
        timeline,
        counter,
        candidate_template(
            request_token_budget=settings.request_token_budget,
            response_token_reserve=settings.response_token_reserve,
        ),
    )
    candidate_prompt = candidate_template(
        request_token_budget=settings.request_token_budget,
        response_token_reserve=settings.response_token_reserve,
    )
    candidates = recognize_candidates(
        timeline, chunks, candidate_prompt, client, concurrency=settings.concurrency,
    )
    evidence = analyze_evidence(
        timeline,
        candidates.candidates,
        counter,
        EvidenceTemplate(
            request_token_budget=settings.request_token_budget,
            response_token_reserve=settings.response_token_reserve,
        ),
        client,
        concurrency=settings.concurrency,
    )
    lifecycles = analyze_lifecycles(evidence.services, source.start_index, source.end_index)
    recipes = build_recipes(
        timeline,
        lifecycles,
        evidence.services,
        timeout_sec=settings.preparation_timeout_sec,
    )
    manifest = build_manifest(source.document(), lifecycles, recipes, timeline)
    report = {
        "schema_version": REPORT_SCHEMA,
        "source": source.document(),
        "model": {"model": config.llm.model, "base_url": config.llm.base_url},
        "prompt_versions": {
            "candidates": CANDIDATE_PROMPT_VERSION,
            "evidence": EVIDENCE_PROMPT_VERSION,
        },
        "tokenizer": counter.name,
        "budget": {
            "request_tokens": settings.request_token_budget,
            "response_reserve": settings.response_token_reserve,
            "concurrency": settings.concurrency,
        },
        "timeline": {
            "call_count": len(timeline.calls),
            "first_node": timeline.calls[0].node_index if timeline.calls else None,
            "last_node": timeline.calls[-1].node_index if timeline.calls else None,
            "chunks": [chunk.document() for chunk in chunks],
        },
        "candidates": {
            "count": len(candidates.candidates),
            "items": [candidate.model_dump(mode="json") for candidate in candidates.candidates],
            "diagnostics": [
                diagnostic.model_dump(mode="json") for diagnostic in candidates.diagnostics
            ],
        },
        "evidence": {
            "services": [service.document() for service in evidence.services],
            "diagnostics": [
                diagnostic.model_dump(mode="json") for diagnostic in evidence.diagnostics
            ],
        },
        "lifecycles": [lifecycle.document() for lifecycle in lifecycles],
        "recipes": [recipe.document() for recipe in recipes],
        "manifest": manifest.model_dump(mode="json"),
    }
    return manifest, report, timeline, source


def run_preprocess(
    source_path: str | Path,
    *,
    run_id: str | None,
    config: PreprocessConfig,
    output: str | Path,
    client=None,
    counter: TokenCounter | None = None,
) -> PreprocessResult:
    output = Path(output).absolute()
    source = Path(source_path).absolute()
    outside_store(source, output)
    if source.resolve().is_relative_to(output.resolve()):
        raise ValueError("preprocess output cannot contain the source Record store")
    if output.exists():
        raise FileExistsError(f"preprocess output already exists: {output}")
    own_client = client is None
    if client is None:
        client = OpenAIChatClient(config.llm)
    try:
        manifest, report, _, _ = analyze(
            source, run_id=run_id, config=config, client=client, counter=counter
        )
    finally:
        if own_client:
            client.close()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    write_json(output / "report.json", report)
    write_manifest(output / "services.json", manifest)
    return PreprocessResult(manifest=manifest, report=report, output=output)
