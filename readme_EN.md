# PalinTrace

Execution trace recording, historical replay, and branching from saved nodes for coding agents.

PalinTrace saves model interactions and tool results alongside the agent context and file state at each step. From a saved node, you can restore the environment and execute the recorded operations that follow, or let the model continue the restored conversation on a new branch.

- **Record · Trace recording (MVP implemented)**: Record model interactions, tool calls, and results together with the corresponding context, task files, and runtime configuration. These records provide the basis for inspecting history, restoring environments, and historical replay.
- **Replay · Historical re-execution (MVP implemented)**: Restore a committed source node and execute subsequent tools from a fixed history without model requests or credentials. Save actual results and snapshots; stop on a definite exit-code mismatch or a framework failure. Full consistency checking is planned as separate Verify work.
- **Resume · Fork and continue (Python MVP)** restores the environment and full conversation at a saved node. The model makes new decisions, tools run in the same container, and each complete step becomes a node on the new branch.

## Demo (Chinese)

https://github.com/user-attachments/assets/c46a93c8-3cd9-43a4-b142-0c557c4bb7b7

[Download the demo (MP4, 2.8 MB)](docs/media/palintrace-demo-zh.mp4?raw=true)

## Quickstart

Run the following commands from the project root. After installation and configuration, use the script to start Record. Its traces and snapshots can be used for both Replay and Resume.

### 1. Install dependencies

Start Docker, install the project dependencies, and build the Btrfs tools and launcher images:

```bash
uv sync --locked --extra mini --extra docker --extra swebench

docker build -f docker/btrfs/Dockerfile \
  -t palinreplay/btrfs-tools:0.1 docker/btrfs

docker build --platform linux/amd64 -f docker/btrfs/Dockerfile.bootstrap \
  -t palinreplay/btrfs-bootstrap:0.1 docker/btrfs
```

Copy the [model configuration template](config/llm/llm.example.toml):

```bash
cp -n config/llm/llm.example.toml config/llm/llm.toml
```

Set `model`, `base_url`, and `api_key` in `config/llm/llm.toml`.

### 2. Start Record

```bash
bash scripts/quickstart_record.sh
```

The script prepares an Astropy task, preloads C0, records the run, verifies restoration, exports the trace, and generates a visualization.

By default, it calls a real model and runs at most two steps. Outputs are grouped by type under `outputs/`, with names starting with `YYMMDD_HHMMSS_`. All stages in the same invocation share a timestamp. Use the script's `--output DIR` to change the output root; see `--help` for other options.

### 3. Preprocess (service manifest)

Analyze an existing Record to extract background-service startup commands and lifecycle intervals. The generated `services.json` supports environment restoration for Replay and Resume. This step calls a model.

Copy the [preprocessing model configuration template](config/llm/preprocess_llm.toml.example):

```bash
cp -n config/llm/preprocess_llm.toml.example config/llm/preprocess_llm.toml
```

Set `model`, `base_url`, and `api_key` in the config. Set `PALIN_RUN` to the recording directory produced in the previous step:

```bash
PALIN_RUN="/path/to/record"
.venv/bin/palin preprocess "$PALIN_RUN"
```

The manifest and analysis report are saved under `outputs/preprocess/YYMMDD_HHMMSS_TASK/` by default. The command prints the output path.

### 4. Replay

Restore a saved node, execute subsequent historical operations without model calls, and save new results and snapshots while preserving the original Record. Set `PALIN_SERVICES` to the `services.json` generated above:

```bash
PALIN_SERVICES="/path/to/services.json"
.venv/bin/palin replay "$PALIN_RUN" --from C0 \
  --services-config "$PALIN_SERVICES"
```

This example runs from C0 to the recorded end. Use `--from` and `--to` to select a range. Results default to `outputs/replay/YYMMDD_HHMMSS_TASK/`; an exit-code mismatch stops execution and preserves actual results. See the [command reference (Chinese)](docs/命令参考.md#replay) for all parameters and restoration scope.

### 5. Resume a branch from a saved node (Python)

Use the source Record in `$PALIN_RUN`. Set `$PALIN_SERVICES` to the service manifest path if you have one, or leave it empty. This example starts at C0 and allows up to two new model decisions. To start at C20, choose a recording that contains that node and change `"C0"` to `"C20"`.

```bash
.venv/bin/python - "$PALIN_RUN" "${PALIN_SERVICES:-}" <<'PY'
import sys

from palinreplay.llm import LLMConfig
from palinreplay.resume import (
    decide_resume_start, restore_resume_start, run_resume_steps, select_resume_start,
)

source, services = sys.argv[1:]
llm_config = "config/llm/llm.toml"
model = LLMConfig.load(llm_config).model
start = select_resume_start(source, "C0", model=model, max_steps=2)
with restore_resume_start(start, services_config=services or None) as session:
    print("Output", session.output, flush=True)
    first = decide_resume_start(session, llm_config=llm_config)
    nodes = run_resume_steps(session, first, llm_config=llm_config)

if nodes:
    last = nodes[-1]
    print("Run", last.run_id, "node", f"C{last.index}", last.node_id)
print("Stop reason", session.report["recording"]["stop_reason"])
PY
```

Use `instruction=` on `decide_resume_start()` to append a user message to the source conversation. The first decision counts toward `max_steps`. Each later request includes the full branch conversation and all previously accepted tool results. Once the whole tool group has executed and its results have been accepted, the recorder commits the node and its file snapshot. The [single-step API](docs/resume-stage5-acceptance.md) `record_resume_step()` is also available for executing one saved decision.

File restoration covers `rootfs-files-v1`. If you supply a manifest, the session prepares the services selected for the starting node in the restored container after the R1 check. Each v2 startup command runs once. Legacy configurations can also specify readiness checks. Omit the manifest to skip preparation.

Session directories are created under `outputs/resume/` by default. Set `output=` to use a new directory outside the source recording. The session's `report.json` identifies the source, completed rounds, last node, stop reason, and cleanup result. The `record/` directory holds the calls and updated conversation at each branch node. The inherited prefix is available in `session.context`. The R1 evidence is in `restore-report.json`, and `preparation.json` records any service preparation requested through a manifest. On submission, budget exhaustion, interruption or failure, committed nodes and calls that already occurred remain available. Exiting the session releases the temporary container and writable workspace. The caller owns any Docker client it supplies.

In the live run from C20, the model submitted in round 10 at branch C30. See the [stage 6 acceptance summary (Chinese)](docs/validation/resume-stage6-20261106.md) for the report fields, the snapshot inspection command and other stopping cases.

## Getting Started

The following examples share a timestamp across stages and can be copied directly:

```bash
PALIN_STAMP=$(date +%y%m%d_%H%M%S)
PALIN_RAW="outputs/download/${PALIN_STAMP}_swebench_lite"
PALIN_TASKS="outputs/convert/${PALIN_STAMP}_swebench_lite"
PALIN_PRELOAD="outputs/preload/${PALIN_STAMP}_astropy__astropy-12907"
PALIN_RUN="outputs/record/${PALIN_STAMP}_astropy__astropy-12907"
PALIN_PREPROCESS="outputs/preprocess/${PALIN_STAMP}_astropy__astropy-12907"
PALIN_REPLAY="outputs/replay/${PALIN_STAMP}_astropy__astropy-12907"
```

You can also omit `--output` to generate a timestamped path under `outputs/<type>/` in the current directory. Each command prints its output path. An explicit `--output` uses the supplied path; existing results are not overwritten.

### 1. download dataset: Download task data

Download the raw SWE-bench Lite dataset, preserving task contents, dataset version, and source information.

```bash
.venv/bin/palin download dataset \
  --source swebench_lite \
  --output "$PALIN_RAW"
```

The outputs are the raw data files and `fetch.json`, which the next stage uses for conversion.

### 2. convert: Create a task package

Select an Astropy task from the raw data and convert it into PalinTrace's task format, organizing the problem description, environment configuration, image references, and evaluation materials.

```bash
.venv/bin/palin convert \
  --input "$PALIN_RAW" \
  --instance-id astropy__astropy-12907 \
  --output "$PALIN_TASKS"
```

The output is a task package containing `manifest.json`. Conversion saves image references; the next stage prepares the actual images.

### 3. download mirror: Prepare task images

Pull Docker images for the task package, verify their platform and actual image identity, and display download progress.

```bash
.venv/bin/palin download mirror "$PALIN_TASKS"
```

On success, the command creates `images.json` in the task package, recording the pinned image information used by preload and record.

### 4. preload: Build the C0 base

Read the task configuration, create a temporary container, complete initialization, and freeze the initial filesystem state as a reusable, read-only C0 base.

```bash
.venv/bin/palin preload "$PALIN_TASKS" \
  --output "$PALIN_PRELOAD"
```

On success, the command returns a `preload_id` and the base path for subsequent recordings. By default, it preserves the image's existing environment. Supply additional initialization commands with `--init`.

### 5. record run: Start recording

Create an independent task container from C0, run the agent, and record model interactions, tool calls, context, and the filesystem state at each step.

```bash
.venv/bin/palin record run "$PALIN_TASKS" \
  --preload "$PALIN_PRELOAD" \
  --output "$PALIN_RUN" \
  --max-steps 2
```

`--preload` selects the base from the previous stage, and `--max-steps` sets the step budget. The recording reuses C0 and saves later filesystem states, such as C1 and C2, as incremental Btrfs snapshots. Outputs include `report.json` and `record/`.

You can reuse the same base for multiple recordings of the task. Run `record run` again with a new `--output` directory; there is no need to repeat the preparation stages.

### 6. Inspect, verify restoration, and export

Inspect the run, check material integrity, and view a saved node:

```bash
.venv/bin/palin record inspect "$PALIN_RUN" --check
.venv/bin/palin record inspect "$PALIN_RUN" --node C0
```

Restore C0 in an independent container, compare the restored files, clean up the verification resources, and generate a verification report:

```bash
.venv/bin/palin record verify-restore "$PALIN_RUN" \
  --node C0
```

Replace `C0` with an actually committed node such as `C1` or `C2`. `inspect --check` checks material digests and snapshot identity; `verify-restore` performs a full comparison of the restored files.

Export a readable trace:

```bash
.venv/bin/palin record export "$PALIN_RUN"
```

The exported recording relies on a separate Btrfs storage pool. Keep the recording directory, the base, and that pool for restoration verification, Replay, and Resume. Replay exports declare their external dependencies, so also retain the original source Store, artifacts, and snapshots. See the [command reference (Chinese)](docs/命令参考.md) for complete options.

### 7. record visualize: Generate a visualization

```bash
.venv/bin/palin record visualize "$PALIN_RUN"
```

The page is saved to `outputs/visual/<timestamp>_<task_id>.html` by default. Open it in a browser to inspect nodes, context, calls, timings, and Btrfs snapshot information. Use `--output FILE` to choose another location.

The playback controls at the bottom support pause, restart, speed selection, and looping, keeping the timeline and details in sync as records advance. This viewer plays back saved records; it does not run the agent or restore containers.

### 8. preprocess: Generate a service manifest

Analyze the completed Record to extract background-service startup commands and lifecycle intervals. The manifest supports environment restoration for Replay and Resume.

Copy the dedicated [model configuration template](config/llm/preprocess_llm.toml.example):

```bash
cp -n config/llm/preprocess_llm.toml.example config/llm/preprocess_llm.toml
```

Set `model`, `base_url`, and `api_key` in the config, then run:

```bash
.venv/bin/palin preprocess "$PALIN_RUN" \
  --output "$PALIN_PREPROCESS"
```

This step calls a model and writes `$PALIN_PREPROCESS/services.json` and an analysis report. Replay and Resume can use the same manifest to select services at different starting nodes.

### 9. replay: Restore and re-execute history

Use the original recording and the generated service manifest to restore C0 and execute through the recorded end:

```bash
.venv/bin/palin replay "$PALIN_RUN" \
  --from C0 \
  --services-config "$PALIN_PREPROCESS/services.json" \
  --output "$PALIN_REPLAY"
```

Use `--from` and `--to` to select another saved range; the manifest selects services for the starting node. Replay makes no model calls and saves actual results and snapshots under `$PALIN_REPLAY`, preserving the original recording. An exit-code mismatch stops execution and retains completed work.

Inspect the new recording and check its materials:

```bash
.venv/bin/palin record inspect "$PALIN_REPLAY" --check
```

Only actually completed steps produce local nodes; starting from source C0 makes C1 the first new node. See the [command reference (Chinese)](docs/命令参考.md#replay) for all parameters and restoration scope.

## Project Structure

```text
src/palinreplay/      CLI, preload, record, storage, and integrations
tests/               Automated tests organized by feature
scripts/             Record quickstart script
examples/            Usage examples and trace visualization
docker/btrfs/        Tools and launcher images
config/llm/          Model configuration template
docs/                Command reference and current validation evidence
outputs/             Local outputs grouped by type and timestamp (Git-ignored)
```

## TODO

- [x] Replay MVP: Restore a committed source node, pass R1 and explicitly configured service preparation, and execute fixed historical tools without model requests. Reference the source baseline, save only actually completed steps, and use definite historical exit codes to control continuation while retaining actual evidence.
- [ ] Verify: Independently check actual Replay results against the pinned source; no Verify command is implemented yet.
- [x] Resume (Python MVP) supports restoring a saved node, multi-turn decisions, branch snapshots, and a final report.
- [ ] Resume command-line entry point.
- [ ] Rewrite: Generate and actually execute replacement operations for a selected historical interval, validate its exit conditions and context handoff, and save a separate derived trace while preserving the original Record.
- [ ] Incremental storage management: Merge and split storage increments while retaining historical nodes.
- [ ] Portable backups and migration across machines.
- [ ] Btrfs storage pool expansion and reference reclamation.
- [ ] Full task-solving evaluation and validation across more tasks and environments.
