# Resume stage 5 acceptance

Stage 5 consumes the fourth stage's saved `ResumeDecision` in the same open session. It executes the pending tools in order even when the decision budget is exhausted, admits their actual returns and publishes one independent branch node for a complete step. Stage 5 makes zero new model requests.

```python
from palinreplay.resume import (
    select_resume_start, restore_resume_start, decide_resume_start, record_resume_step,
)

start = select_resume_start(source, "C20", model="openai/M", max_steps=1)
with restore_resume_start(start, output=output, services_config=services) as session:
    decision = decide_resume_start(session, llm_config=llm_config)
    node = record_resume_step(session, decision)
```

The entry accepts one saved decision per active session. It checks messages, assistant response, raw response, ordered tool IDs/arguments and accounting against private copies of the fourth-stage evidence. A successful claim is persisted before execution and cannot be retried after failure. Each real tool return is persisted before the next tool starts. Native mini observation formatting and Record admission preserve actual output, exit code and message positions. Only a fully admitted group reaches `Recorder.commit_step()`.

A native submission stops tool execution immediately and delivers the submitting tool's `exit` message. A single-tool submission produces a complete node. In a multi-tool group, earlier results remain recorded, later tools are skipped, and `record_resume_step()` returns `None`. The RunEnd and the recording and session reports mark the group `incomplete`. Tool message templates use the selected branch model name.

The branch Store lives at `session.output / "record"`. Its protected config pins the source Store, Run, Node, State and original encoded materials. Its first local node has index `source.index + 1`, independent Run/Node IDs and no external ID in the local parent fields. Resume has no pinned historical endpoint; Replay retains its endpoint checks. The source Record and Btrfs pool remain retained dependencies. Independent recovery means recovery after the execution container has been removed.

Failures preserve the call tail. The final report distinguishes fourth-stage decision success from recording failure, including errors caught by the caller. Published-node counts are read from the Store; a post-publication error retains the actual node, while unavailable publication facts are reported as unknown.

## Task commits and verification

The worktree started clean on `palin-resume-stage5`. Each task was implemented and checked before its own local commit.

| Task | Commit | Verification at that task |
| --- | --- | --- |
| 1. Claim and validate one saved decision | `de7feaa` | 69 passed; mutation, closed, repeated and concurrent consumption rejected |
| 2. Open an independent source-indexed branch | `ed690fc` | 136 passed, 3 skipped; no source successor required; Replay range checks preserved |
| 3. Associate the saved model answer and tool group | `860785d` | 112 passed; original request/response/accounting and ordered IDs retained; no model dispatch |
| 4. Execute tools and persist each real return | `6a28ffe` | 60 passed; real Astropy C0 probe: model 1, tools 2, nodes 0, one marker line |
| 5. Admit native mini tool messages | `dd07023` | 57 passed; 45/46-message C20 cases, exit 7, incomplete groups rejected |
| 6. Publish an independently restorable node | `e538fd0` | 85 passed; real C20 → C21, model 1, tools 2, node 1, recovery in another container |
| 7. Preserve failures and actual publication facts | `dd7da46` | 343 passed; interrupt/admission/snapshot faults, post-publication error and unknown Store facts |
| 8. Full real matrix and final regression | This acceptance commit | 1781 passed, 3 skipped; all 11 real scenarios passed; additional source batch 3/3 |

Counts overlap and must not be added. Ruff and `git diff --check` passed at each implementation commit. The task 4 component probe first omitted model-answer admission; Recorder rejected it before executing a tool. The corrected probe passed in a fresh session. Its initial failure remains in the ignored local evidence.

## Fixed sources

| Case | Source Run | Source Node | Start → expected new node | Prefix messages |
| --- | --- | --- | --- | ---: |
| C20 services | `a9261e45064b44029fa21e2cd2c3635e` | `c18d313c6bb04ac98b1ebba48f145ba3` | C20 → C21 | 42 |
| Astropy 14182 | `fb0e1125841c4877822043bac426d330` | `42a6f209f15b4aa4be8d0c39b94e7839` | C0 → C1 | 2 |
| Django 10914 | `545c3c7c0db2402ab22d0a54c2f57e1a` | `4ea52cca2a3a40169cd1c4f1d86c2571` | C1 → C2 | 4 |
| Astropy 12907 long | `577a3cd21a1344beb69e3fceb4cb8695` | `d525dddcf31247b8be8eeeb70948d5bf` | C22 → C23 | 49 |

All cases set `max_steps=1`. The controlled fourth-stage model response appends a line to a unique, initially absent file and then reads it. Model transport is controlled; the containers, actual commands, recording, native admission, Btrfs snapshots and subsequent recovery are real. Service startup and acceptance probes are counted separately.

The C20 v2 service manifest is:

`../archives/261104_141057_palin-preprocess-services/outputs/validation/261104_123158_single_pass_preprocess_replay/preprocess/services.json`

Its SHA-256 is `241a072f9c145ab28bf7c455b8a9e306034ea397964a9eba36ef8fe68437887e`. Only A and B are prepared for C20.

## Reproduction and evidence

Run from this worktree with Docker and the retained source pools available. Use a new output directory on every invocation:

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True TERM=xterm-256color .venv/bin/python -B \
  -m examples.resume_record_validation \
  --output outputs/resume/NEW-stage5-acceptance \
  --services-config ../archives/261104_141057_palin-preprocess-services/outputs/validation/261104_123158_single_pass_preprocess_replay/preprocess/services.json

LITELLM_LOCAL_MODEL_COST_MAP=True TERM=xterm-256color .venv/bin/python -m pytest -q -ra \
  --junitxml=outputs/resume/NEW-stage5-checks/pytest.xml
.venv/bin/ruff check src tests examples
git diff --check
```

Local evidence is ignored by Git. The final run writes per-case restore, preparation, decision and recording reports, commands, actual returns, container IDs, branch IDs, recovery results, source inventories and pool audits to `outputs/resume/261105_stage5_final_verified/`. Full regression logs (`pytest-final.log`, `pytest-final.xml`), their hashes and verification JSON are under `outputs/resume/261105_stage5_checks/`. Earlier real component evidence is under `261105_stage5_task4/` and `261105_stage5_task6/`; the first complete matrix is retained under `261105_stage5_final/`.

The controlled fourth-stage request count is 1 and its actual provider HTTP request count is 0. Stage 5's model-request count is 0. Service startup and acceptance probes are separate from the two tools. In the machine summary, `recording_operations`, `agent_runs` and `task_file_updates` are counters from the fourth-stage guard only; the recording stage's tools, snapshots and committed nodes have their own measured counts.

## Final regression

The final full suite passed: **1781 passed, 3 skipped, 0 failed**, in 153.68 seconds. The three skips are existing ordinary-Record parameter cases without an external source in `tests/record/test_replay_verification_scope.py`; they do not skip any of the four real acceptance sources. There are two warnings, from LiteLLM event-loop deprecation and pytest's JUnit `record_property` compatibility. Repository-wide Ruff and `git diff --check` passed.

The initial full suite exposed eight failures in the source-cache read-count tests. Resume and Replay initially used separate cache keys and read the same protected config twice in one snapshot. The final implementation caches a single typed source reference for both queries, preserves cached `None` for ordinary Records, and clears the cache at the original snapshot boundary. The existing cache/invalidation tests and new mixed Resume/Replay query test passed in a 61-test focused run; the entire suite was then rerun successfully. Initial and final logs are retained separately.

## Final real results

**Stage 5 passed on 2026-11-05.** The final matrix ran with the corrected shared source reader in **431.51 seconds**, exited 0 and recorded `status=passed`, `stage5_complete=true`, and `additional_passed=3` of `additional_total=3`. All 11 required scenarios passed. The first complete matrix also passed in 490.61 seconds and remains available separately.

Each of the four required sources produced exactly **1 fourth-stage model request, 0 fifth-stage model requests, 2 actual tool executions and 1 complete node**. Each unique marker contained one line. The original execution container was removed before another container restored the new node and read the matching marker.

Paths in this table are relative to `outputs/resume/261105_stage5_final_verified/`.

| Case | Branch | Branch Run | Branch Node | Messages | Recovery | Output directory |
| --- | --- | --- | --- | ---: | --- | --- |
| C20 services | C21 | `8409e8cb3205489eb3428a2da19109f2` | `7128857a284e4b809259d8a9aadb37e6` | 45 | Passed | `c20/` |
| Astropy 14182 | C1 | `0632bb4df85f4c9b8d0176787581b64c` | `9c664d49ef494172b668b7f76c8890d8` | 5 | Passed | `astropy_initial/` |
| Django 10914 | C2 | `93189a338b12426b8e64bfadd17ee083` | `853d7a27670f42d2a6c0058e11d4ed86` | 7 | Passed | `django_middle/` |
| Astropy 12907 long | C23 | `b0bdd5661c8841bd97a2585c83408e6a` | `41ea4541b5e44a66934c8f75e447519f` | 52 | Passed | `astropy_long/` |

The original C20 and parent C21 snapshots retained their exact audited identities and read-only flags. All four source recordings retained identical file hashes and node metadata. The service manifest hash was unchanged. Output Stores contain only each case's own new node; container, root-volume and temporary-workspace cleanup was confirmed, and pool workspaces matched their baselines.

| Boundary | Actual result |
| --- | --- |
| Duplicate consumption | Rejected in every successful case; no extra tool, snapshot or node; marker remains one line |
| Modified command | Rejected before Store creation or tool execution; original fourth-stage evidence unchanged |
| Closed unconsumed session | Rejected after real container cleanup; model 1, tools 0, nodes 0 |
| C20 with instruction | Independent C21, 46 messages, two real returns and successful recovery (`guided/`) |
| Ordinary nonzero return | Second command reads the marker and exits 7; actual output and code 7 are admitted; one recoverable node (`nonzero/`) |
| Interrupted group | First command actually executes and returns; second is interrupted at executor entry; one real return, one unknown recorded attempt, zero complete nodes, retry rejected (`interrupt/`) |
| Result admission failure | Both actual returns remain queryable; zero complete nodes and no replay (`adoption_failure/`) |
| Failure before snapshot | Both returns already admitted; zero snapshots and complete nodes; failure recorded, retry rejected (`snapshot_failure/`) |
| Parent ends at branch point | The single-node Astropy branch C1 is used as a parent with no successor; its new independent C2 restores successfully (`last_parent/`) |

The final `last_parent` source is Run `0632bb4df85f4c9b8d0176787581b64c`, Node `9c664d49ef494172b668b7f76c8890d8`; its new Run is `e06de90e1d274f58bef64cc2a196c56e`, Node `305ddd835fdb4e35a12495879aed707b`. This avoids altering or truncating original sources whose final nodes contain terminal messages.

For interruption, the recording report counts two executor-entry attempts while the acceptance guard confirms only one command reached the real executor. The second recorded result is explicitly unknown. Failure-case `status=passed` in the acceptance summary means the expected failure and evidence/cleanup checks passed; their session reports correctly remain failed or interrupted. Post-publication errors and unreadable Store facts were additionally verified by automated fault tests.

All eight meta tasks are complete. There are no blocked or unfinished acceptance items and no additional requirements for this group. The automatic multi-round decision loop remains outside this one-step implementation.

## Post-review regression

In `outputs/resume/261105_224340_stage5_review_fixes/`, the two real multi-tool submission cases accepted the native exit message and ended `incomplete` with zero nodes. One tool executed when the submitting command came first, and two executed when it came last. A controlled comparison with Record matched the tool events, accepted messages and RunEnd status in both orders.

All four source recordings produced and recovered their expected nodes (C21, C1, C2 and C23), each with the selected branch model name in its template variables. A focused test also checked the rendered tool message after a model switch. In the full suite, 1,784 tests passed and 3 were skipped.
