# Resume stage 4 acceptance

Stage 4 obtains one model decision from a restored, service-ready C20 session. The returned commands remain pending for stage 5. The Python entry point is:

```python
from palinreplay.resume import decide_resume_start, restore_resume_start

with restore_resume_start(start, services_config=services_path) as session:
    decision = decide_resume_start(
        session, instruction="下一步先检查 B 的日志", llm_config=config_path,
    )
    # decision.assistant_message, raw_response, actions and accounting
    # Stage 5 can consume these actions in this same session.
```

The optional instruction is appended to a deep copy of the saved messages. The selected `start.model` overrides the parent/configured model. A public LiteLLM request is persisted before calling the connection layer; API credentials are excluded. The same response supplies the assistant, ordered full tool arguments and usage. `ResumeDecisionError.category` distinguishes `config`, `request` and `tool_format`; the exception's `decision` and the session report retain acquired evidence.

Source fixture: Run `a9261e45064b44029fa21e2cd2c3635e`, C20 Node `c18d313c6bb04ac98b1ebba48f145ba3`, State `1379ebee3b81426494c547ce5676c1a7`, from `261102_150636_service-overlap-40`. The existing v2 service manifest selects A and B. The source has 41 complete nodes (C0–C40), a 42-message C20 prefix, and 20 inherited model calls.

The opt-in runner is `python -m examples.resume_decision_validation`, with required `--source`, `--services-config`, `--output`, `--model` and `--llm-config` arguments. For an OpenAI-compatible gateway, specify `openai/<gateway model>` explicitly. It restores separate default, guided and real-model sessions, prepares and probes A/B, then guards each decision against tools, task-file changes, snapshots and complete-node commits. Restoration, read-only inspections and resource cleanup are outside that interval. Detailed local evidence lives outside the source recording under `outputs/resume/` and is not versioned.

Fresh acceptance on 2026-11-05 passed in 135.40 seconds. Evidence: `outputs/resume/261105_stage4_final/acceptance.json`, with per-session `report.json`, `restore-report.json` and `preparation.json` under `default/`, `guided/` and `real/`. No API key occurs in the acceptance JSON files.

| Case | Input messages | Model calls | Actual HTTP calls | Pending actions | Cleanup |
| --- | ---: | ---: | ---: | ---: | --- |
| Controlled default | 42 | 1 | 0 | 2, in response order | Confirmed |
| Controlled instruction | 43 | 1 | 0 | 2, in response order | Confirmed |
| Real instruction | 43 | 1 | 1 | 3, in response order | Confirmed |

All three sessions passed R1 and prepared A/B, with successful HTTP probes before deciding. Each kept historical calls at 20, recorded one new successful decision and reduced the independent budget from 2 to 1. Tool executions, task-file updates, file snapshots, complete-node commits, recording operations and ordinary Agent runs were zero during each decision. The controlled marker commands remained pending and no marker file appeared. Before/after task inventories matched. Source files, all 41 source nodes, the service manifest and the read-only source snapshot remained unchanged; temporary resources and pool mounts were released.

The selected LiteLLM model was `openai/commandcode/deepseek/deepseek-v4.1-flash:max[1m]`; the HTTP request and response identified `commandcode/deepseek/deepseek-v4.1-flash:max[1m]`. The real assistant answered:

> I’ll inspect the B service directory and any available log/state files first, without stopping the running services.

The actual pending commands, in order, were:

```bash
find /opt/palin-replay-service-fixture/B -maxdepth 2 -type f -printf '%p\t%TY-%Tm-%Td %TH:%TM:%TS\t%s bytes\n' | sort
find /opt/palin-replay-service-fixture/B /testbed/.palin-service-demo -maxdepth 3 \( -iname '*log*' -o -iname 'service.json' \) -printf '%p\t%y\n' | sort
cat /opt/palin-replay-service-fixture/B/service.json 2>/dev/null || echo 'B service.json not found'
```

Their original IDs, complete arguments and raw response are retained in `real/report.json`. None was executed. The response reported 17,756 prompt tokens and 439 completion tokens (18,195 total); cost could not be determined and is recorded as unknown.

Final related regressions: **628 passed, 0 failed, 0 skipped**, with two LiteLLM/pytest warnings. Logs and JUnit are under `outputs/resume/261105_stage4_verification/`. Ruff over `src tests examples` and `git diff --check` passed. The regression selection covered Resume, mini, LLM, Record run, Replay and preprocessing manifest tests. Failure coverage verified configuration errors before transport, a single HTTP 500 attempt, retained response/usage on tool-format errors, no partial action delivery and cleanup of a prepared session.
