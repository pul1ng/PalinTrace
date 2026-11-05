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
| 10. Real C20 four-round execution | `074e2f4` | Passed in 45.56 s; 4 model requests, 4 real tools, 4 Btrfs captures, C21–C24, one budget-paused RunEnd |
| 11. Independent branch-node recovery | `8ea2792` | C21/C24 both passed in new containers after execution-container deletion; exact files, messages and input positions; 46.63 s |
| 12. Cross-source multi-turn regression | `e89d06e` | All 3 sources passed; 2 requests/tools/nodes per source; all 6 new nodes independently restored |
| 13. Long-run and final acceptance | This acceptance commit | Controlled 100 passed; starts 3/3 and real fault boundaries 4/4 passed; 17 node recoveries; Resume 302 passed, full 1809 passed / 3 skipped; live-model service faults recorded separately |

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

## Task 11: independent C21 and C24 recovery

The recovery command opens the persisted branch Store after independently confirming that the execution container is absent. It restores each selected node in a new container and compares the exact saved context and input position as well as the marker file. Evidence is written separately to `outputs/resume/261106_004858_multiturn/main4-recovery/`.

```bash
TERM=xterm-256color LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -B \
  -m examples.resume_multiturn_validation \
  --output outputs/resume/NEW-multiturn/main4-recovery \
  --recover-from outputs/resume/261106_004858_multiturn/main4
```

Both recoveries passed. C21 used container `b2908a7600b5784478c049855185f75599ed2939014808f2091e6be5875cb408` and contained only the first UUID marker, with 45 messages and `next_message=45`. C24 used container `d79be49d3cfab8fcdc98549c1a76319053a5a2be4a2e347a2dff9582b2abbdf9` and contained all four ordered markers, with 51 messages and `next_message=51`.

Each recovered context and complete input-position record exactly matched its node's saved payload. Both restorations identified Run `db1f191d188d4c89983c563d8c7cbc4c` as their branch, and both containers differed from the removed execution container and from each other. Cleanup was independently checked; the branch material inventory was unchanged after both reads. Total recovery time was 46.63 seconds.

## Task 12: three additional source Records

The same controlled-model path now accepts the three other pinned sources from `examples/resume_record_validation.py::SOURCES`. Each runs two rounds, audits the complete source prefix and local parent chain, and independently restores both new nodes after removing its execution container. No service manifest is needed for these source cases.

All three cases passed under `outputs/resume/261106_004858_multiturn/`, with separate execution and recovery directories for `astropy_initial`, `django_middle` and `astropy_long`.

| Source / start | Branch Run | New nodes | Request messages | Saved messages | Restored nodes |
| --- | --- | --- | --- | --- | --- |
| Astropy 14182 / C0 | `30e9d5845cad494597dffcb4db7d862e` | C1 → C2 | 3, 5 | 5, 7 | C1, C2 passed |
| Django 10914 / C1 | `3bceb31baa45449e96ec75b3837be52e` | C2 → C3 | 5, 7 | 7, 9 | C2, C3 passed |
| Astropy 12907 / C22 | `35e1b2a7e84b4e3180126383ffdd80aa` | C23 → C24 | 50, 52 | 52, 54 | C23, C24 passed |

The node IDs are respectively `4e39033ee2164929814bd34af4982c88` → `bfef29816bdd4bc68c20b4d942bf9d74`, `df981c150a08416caf630f3b36f9dfb7` → `44519025a4b7409596b4209a6e6dee78`, and `aac77b820bf64bc3b023a33072d4fa93` → `3745468afdc14d44b1eb9483837c0de7`.

Each case made exactly two model requests and two real tool calls, captured two Btrfs boundaries, and saved two complete nodes with one budget-paused RunEnd. Source references, complete conversation prefixes and local parent links passed. All six nodes recovered the expected marker prefix, exact saved context and input position after their execution containers were removed. All three source file/node inventories remained unchanged, and execution/recovery cleanup was confirmed. Execution plus recovery took 69.28, 66.98 and 77.00 seconds respectively.

```bash
TERM=xterm-256color LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -B \
  -m examples.resume_multiturn_validation --case astropy_initial \
  --output outputs/resume/NEW-multiturn/astropy_initial
```

Use `--case django_middle` or `--case astropy_long` for the other two sources; use `--recover-from` with a fresh output directory to restore the two nodes.

## Task 13: long runs, multiple starts and final evidence

The additional opt-in experiments are in `examples/resume_multiturn_experiments.py`, with modes `controlled100`, `real100`, `starts` and `faults`. Each mode requires a fresh output directory. The live mode accepts the existing private model configuration via `--llm-config`; requests and HTTP bodies are captured without credentials.

The controlled 100-round experiment passed in 278.94 seconds under `outputs/resume/261106_004858_multiturn/controlled100/`. It restored C20 once, started A/B once each, and performed exactly **100 model requests, 100 real tool calls, 100 Btrfs captures and 100 complete nodes**, with one budget-paused RunEnd. The controlled model made zero provider HTTP requests.

Branch Run `43f1ab54d32b479a9a93191fc9d697f7` begins at C21 `af03b64697594d6abecf155a45685fbe` and ends at C120 `690f07391f1e46f3ac1404e3ecb4fca8`, passing the parent's C40 endpoint. Actual requests grew from 43 to 241 messages; C120 saved 243 messages. Every request and node passed the full-prefix and accepted-call reconstruction checks.

After execution-container deletion, `controlled100-recovery/recovery.json` confirmed independent C120 restoration with 100 ordered file markers, exact saved context and input position, and cleanup. Source files, node references and manifest remained unchanged.

The initial live-model attempt, preserved separately in `real100/`, used the legacy configuration's `openai/deepseek-v4-flash` name. The gateway rejected its first actual HTTP request as an unknown model. It made one model request, zero tools and zero new nodes; the last complete node remained source C20. The request failure and cleanup were recorded in 34.58 seconds.

A second fresh attempt in `real100-verified/` used the previously accepted `openai/commandcode/deepseek/deepseek-v4.1-flash:max[1m]` name. The gateway recognized it but returned `No available providers`. It also stopped at its first request with zero tools/nodes and source C20 as the last complete node; cleanup and source audits passed in 28.79 seconds.

The model catalogue was read once and saved in `model-catalog.json`. A third independent attempt in `real100-catalog/` used its advertised `openai/deepseek-v4.1-flash` name and stopped on the first request with an upstream `Invalid 'Authorization' header or token` error, in 31.00 seconds. It also produced zero successful decisions, tools and new nodes, retained C20 as the last complete node, and confirmed cleanup and source integrity.

Thus the live-model result is **a model-service fault at the first request, with zero complete new rounds and no branch node**. Each of the three isolated attempts sent exactly one model request and one HTTP request; none retried automatically. No submission content or natural stopping round was obtained. A functioning provider and valid upstream authorization are required to obtain a natural model result. These are classified failure results permitted by the acceptance checklist, and are not reported as a successful live-model continuation.

All three same-source starts passed in `starts/starts.json` (141.51 seconds), using source Run `a9261e45064b44029fa21e2cd2c3635e`, the same manifest, controlled model and instruction:

| Start / source Node | Prepared services | Branch Run | New nodes | Actual request messages |
| --- | --- | --- | --- | --- |
| C1 / `866611f8dd0f4e5dbfcc133512838cc0` | None | `3ddcc208c3074664a04d8a0f09b9b2ae` | C2, C3 | 5, 7 |
| C12 / `34744bee9f454be79bdfd119b03ad255` | A once | `9b4d3d07a9664948b3e2ab2d5eae6de9` | C13, C14 | 27, 29 |
| C31 / `6e2e3ddd309f4bc4b85c5b0ef132dc45` | None | `6cc9318931ea4cca992538418c52d5a0` | C32, C33 | 65, 67 |

Each case made two requests and two real tools, saved two complete nodes and paused on budget. C12's real A request returned `PALIN_SERVICE_A_OK`. All three branch Runs and workspaces were distinct. Their independently recovered last nodes contained their own two markers and neither of the other branches' marker files. Source audits and execution/recovery cleanup passed for each case.

All four real fault scenarios passed in `faults/faults.json` (199.38 seconds). Their Resume reports correctly remain failed or interrupted; the acceptance result means the injected boundary, evidence and recovery checks passed.

| Second-round boundary | Model requests | Tool attempts / physical commands | Complete nodes | Actual stop | Independently restored |
| --- | ---: | --- | ---: | --- | --- |
| Model request failure | 2 | 1 / 1 | 1, through C21 | failed, model stage | C21 |
| Tool interruption | 2 | 2 / 1 | 1, through C21 | interrupted; RunEnd cancelled | C21 |
| Snapshot failure before publication | 2 | 2 / 2 | 1, through C21 | failed, commit stage | C21 |
| Error after C22 publication | 2 | 2 / 2 | 2, through C22 | failed, commit stage | C21 and C22 |

Each case made zero third-round requests. Actual Store nodes, report totals and the RunEnd's last node agreed. The interruption preserves an unknown second tool attempt without claiming a second physical command. All five saved nodes recovered the appropriate file prefix, context and input position after execution-container removal. Source file/node/manifest audits and temporary-resource cleanup passed.

Final checks are saved in `checks/verification.json` with command, exit code, duration, log/JUnit paths and SHA-256 hashes:

| Check | Actual result |
| --- | --- |
| Resume tests | 302 passed, 0 failed, 0 skipped |
| Full regression | 1809 passed, 0 failed, 3 skipped |
| Ruff (`src tests examples`) | Passed |
| `git diff --check` | Passed |

The three skips are the existing ordinary-Record parameter cases in `tests/record/test_replay_verification_scope.py` that require an external source. They do not skip any real source used in this acceptance. Resume and full pytest took 41.05 and 138.13 seconds respectively; their counts overlap and must not be summed.

The full machine-readable conclusion is `outputs/resume/261106_004858_multiturn/final-acceptance.json`. It links the evidence JSON files and their SHA-256 hashes, the regression logs and JUnit, and all live-model failure results. Its status is `completed_with_live_model_faults`.

## Final conclusions

- **Complete conversation: passed.** Actual model-call requests matched the full source prefix, the instruction appended once, and all prior accepted branch exchanges. Four-round requests were 43/45/47/49 messages; the controlled long run retained the full history through request 100. Live first-request HTTP bodies were also compared with their public request.
- **Continuous files and environment: passed.** Four-round and 100-round controlled cases each restored once, prepared A/B once and used one execution container/workspace throughout. Later tools observed earlier writes.
- **Independent restoration: passed.** Seventeen saved branch nodes were actually restored in new containers after their execution containers were removed, including C21/C24, C120, all six cross-source nodes, three same-source endpoints and five fault-case nodes. Files, exact contexts and input positions matched.
- **Real-model stopping result: fault.** The live attempts completed zero new rounds and stopped at C20 on their first model requests because of gateway/provider/authorization failures. Natural submission or budget exhaustion with the real model remains unverified and requires a functioning provider with valid upstream authorization.
- **100-round budget: passed with the controlled model.** Exactly 100 requests, tools and nodes C21–C120; no request 101; one budget-paused RunEnd; C120 independently recovered all 100 markers.
- **Multiple starting points: 3/3 passed.** C1/C12/C31 selected the correct prefixes and services and created isolated branches.
- **Cross-source regression: 3/3 passed.** Astropy 14182, Django 10914 and long Astropy 12907 each produced and restored two consecutive nodes.

The 13 meta tasks have implementation or acceptance commits. The remaining external condition is live-model provider availability and authorization; this acceptance does not claim a successful natural live-model continuation. No additional product feature was introduced.
