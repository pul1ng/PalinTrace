# Multi-turn Resume acceptance

Multi-turn Resume keeps one restored container, workspace, branch Store, Recorder and complete conversation for all new decision rounds. The first `ResumeDecision` is imported once; later requests are recorded in their own Step. Each complete group publishes one independently restorable node. The budget counts new decisions, including the first, independently of the source index or endpoint.

```python
from palinreplay.resume import (
    select_resume_start, restore_resume_start, decide_resume_start, run_resume_steps,
)

start = select_resume_start(source, "C20", model=model, max_steps=4)
with restore_resume_start(start, output=output, services_config=services) as session:
    first = decide_resume_start(session, instruction=instruction, llm_config=llm_config)
    nodes = run_resume_steps(session, first, llm_config=llm_config)
```

`nodes` contains the complete nodes returned before budget pause or native submission. Exceptions preserve the actual published prefix and call tail. `record_resume_step()` retains its original `Node | None` result and one-step behavior. Either entry consumes the initial decision only once.

The recording report separates inherited counters from branch activity. `total_model_requests` includes the already obtained first decision; `model_requests` and its explicit alias `recording_model_requests` count requests made during recording. `request_accounting` retains usage and known/unknown cost per request. `known_cost` sums known fees, while `cost` remains null unless every request's cost is known. Node counts and the last complete node are reread from the Store, including when publication succeeds before an exception. Stop reasons distinguish `budget_exhausted`, `submitted`, `incomplete_tool_group`, `failed`, `interrupted` and compatibility entry `step_completed`.

## Sequential task commits

The worktree began clean on `stage-5-5` at `284582b`. Each row was verified and committed before the next task began. Test selections overlap and must not be added.

| Task | Commit | Verification |
| --- | --- | --- |
| 1. Reusable execution and commit of one tool group | `a6c035b` | 35 passed; helper leaves recording open; existing single consumption and failure paths preserved |
| 2. One recording across branch steps | `0babbe1` | 25 passed; C21/C22 local chain and one paused RunEnd; an initial test-field typo was corrected |
| 3. Shared growing branch conversation | `9f88fcb` | 25 passed; source prefix, optional instruction once, ordered tool IDs and input positions |
| 4. Full-history model requests | `60ce2bb` | 51 passed; actual model-call arguments match 43/45/47/49 messages with instruction and 42/44/46/48 without; expected native null field corrected |
| 5. Later model decisions recorded in their own Step | `7b9e9f4` | 57 passed; two actual requests; request and raw response survive dispatch/parse failure |
| 6. Public bounded multi-step runner | `6c53c48` | 23 passed; budgets 1/2, real local shell file continuity, duplicate rejection |
| 7. Native nonzero and task-submission semantics | `becc5e9` | 54 passed; ordinary exit 7 continues; single-tool submission complete; multi-tool submission incomplete; no later model request |
| 8. Failure/interruption and publication reconciliation | `dbdc7ab` | 157 passed; second-round faults and post-publication RunEnd recovery across Resume/Recorder/Replay |
| 9. Cumulative branch report | `d9c3785` | Resume 302 passed; final progress/report-stage assertions 9 passed; variable tool counts and unknown costs retained |
| 10. Real C20 four-round execution | This task's commit | Passed in 45.56 s; 4 model requests, 4 real tools, 4 Btrfs captures, C21–C24, one budget-paused RunEnd |

Ruff and `git diff --check` passed for each completed task. The implementation checks observed existing LiteLLM/Pydantic warnings; final regression totals will be recorded separately.

## Task 10: real four-round C20 execution

The opt-in runner is `examples/resume_multiturn_validation.py`. Its controlled model provides one tool per round; Docker execution, service startup, Btrfs capture, node publication and source audits use the real implementation. The first tool generates a UUID inside the execution container and returns it. Later tools append to and read the same marker file.

The current run writes independent evidence to:

`outputs/resume/261106_004858_multiturn/main4/`

The run passed. C20 was restored once; A and B each started once and returned their expected HTTP response, with no C startup. All four tools ran in container `826f46d29c6ed2b257bf609b3c9b20e02753c2f0228c0326bf0e315226f48a10`. The first tool generated `5dde914f-13d4-407b-a90b-d51df04e95a0` inside that container. Its actual return remains in the fourth request.

The new branch Run is `db1f191d188d4c89983c563d8c7cbc4c`:

| Branch node | Node ID | Request messages | Saved messages |
| --- | --- | ---: | ---: |
| C21 | `c82e511255db4e2a947ea319d8ae9bf8` | 43 | 45 |
| C22 | `8081217a07784327bb77e00f76437e6a` | 45 | 47 |
| C23 | `4aed78bbb6f642ec9b0f933ee9eefb47` | 47 | 49 |
| C24 | `f669612c1c184c31a25ea0c3c6ab771c` | 49 | 51 |

Each request was compared against the source prefix and actual accepted model/tool calls. Each saved context was independently rebuilt from those accepted calls. Local parent IDs link the four nodes in order; the first node refers externally to source C20 through the protected branch configuration. The run published four nodes and one `paused` RunEnd with stop reason `budget_exhausted`.

The source's full file inventory and all 41 node records, including parent C20–C24 identities and context/state references, were identical before and after. The selected source snapshot retained its read-only identity; temporary pool workspaces matched the baseline. The service manifest was unchanged. The execution container and temporary root resources were removed and cleanup was independently checked. Distinct-node recovery is task 11 and is not included in this task's result.

```bash
TERM=xterm-256color LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -B \
  -m examples.resume_multiturn_validation \
  --output outputs/resume/NEW-multiturn/main4 \
  --services-config ../archives/261104_141057_palin-preprocess-services/outputs/validation/261104_123158_single_pass_preprocess_replay/preprocess/services.json
```

Raw outputs are Git-ignored. The runner saves each actual public request, SDK response and tool result, plus an acceptance JSON containing the source and branch references, node chain, recording summary, service/container observations and immutable-source audit.
