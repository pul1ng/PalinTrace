# PalinTrace

Execution trace recording, environment restoration, and historical replay for coding agents.

PalinTrace links an agent's model interactions, tool calls, execution results, context, and task environment state. The goal is to let users select any saved node, restore its associated environment, and replay recorded interactions from that point, keeping playback and environment state aligned with the same position in history.

- **Record · Trace recording (MVP implemented)**: Record model interactions, tool calls, and results together with the corresponding context, task files, and runtime configuration. These records provide the basis for inspecting history, restoring environments, and historical replay.
- **Replay · Historical re-execution (MVP implemented)**: Restore a committed source node and execute subsequent tools from a fixed history without model requests or credentials. Save actual results and snapshots; stop on a definite exit-code mismatch or a framework failure. Full consistency checking is planned as separate Verify work.
- **Resume · Fork and continue (implemented in stages)**: Restore a saved node, validate its context and writable environment, prepare optional services, then obtain one new decision and record its tool group as an independent branch node.

## Demo (Chinese)

https://github.com/user-attachments/assets/c46a93c8-3cd9-43a4-b142-0c557c4bb7b7

[Download the demo (MP4, 2.8 MB)](docs/media/palintrace-demo-zh.mp4?raw=true)

## Quickstart

Run the following commands from the project root. After installation and configuration, use the script to start Record. The resulting traces and snapshots can be used for Replay and provide inputs for future Resume workflows.

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

### 5. Resume: restore a starting node and record one branch step

The Python API restores the saved conversation and writable environment, passes R1,
and prepares optional services. It can then obtain one decision and record the tools
in that same container:

```python
from palinreplay.resume import (
    select_resume_start, restore_resume_start, decide_resume_start, record_resume_step,
)

start = select_resume_start("outputs/record/SOURCE", "C20", model="openai/M", max_steps=1)
with restore_resume_start(start, services_config="outputs/preprocess/SOURCE/services.json") as session:
    decision = decide_resume_start(session, llm_config="config/llm/llm.toml")
    node = record_resume_step(session, decision)
    if node is not None:
        print(node.run_id, node.node_id, node.index)  # independent branch C21
```

Omit `services_config` to skip preparation. A valid manifest selecting no services
completes preparation with zero items. Manifest source, digest, evidence and range
checks happen before creating a task container. After R1, v2 startup commands run
once each without added readiness probes; explicitly configured legacy readiness
remains supported. Preparation failure prevents delivery and cleans up resources.

Outputs default to `outputs/resume/<timestamp>_C20-restore/`. `report.json` preserves
restoration, preparation, caller and cleanup outcomes separately; `restore-report.json`
contains R1 and resource evidence, and `preparation.json` records configured startups.
The context manager releases the container and writable workspace on normal or
exceptional exit. A caller-supplied Docker client remains caller-owned.

`decide_resume_start()` accepts an optional `instruction=` after the inherited prefix.
`record_resume_step()` consumes that saved response once, without another model request,
including when the decision used the last budget slot. Modified decisions, repeated
consumption and closed sessions are rejected before execution. Actual results are saved
as each tool returns; the complete group is admitted before one node is published under
`session.output / "record"`. Ordinary nonzero exits retain their actual output and status.
On native submission, mini accepts the submitting tool's `exit` message. A single-tool submission
publishes a node. When submission cuts a multi-tool group short, the function returns `None`,
the recording and session report `incomplete`, and the Store retains the tools' actual returns.
Interruptions and recording failures preserve the call tail, and `report.json.recording`
reports the actual published node count. `session.context` remains the source prefix;
the new node contains the updated branch messages and input position. This API handles
one round; an autonomous decision loop remains future work. See the
[stage 5 acceptance record](docs/resume-stage5-acceptance.md) for evidence and reproduction.

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

The export does not include the Btrfs storage pool. Keep the recording directory, the base, and their associated storage pool for restoration verification, Replay, and future Resume. When exporting Replay results, also retain the original source Store, artifacts, and snapshots; the export declares these external dependencies without packaging them. See the [command reference (Chinese)](docs/命令参考.md) for complete options.

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

This step calls a model and writes `$PALIN_PREPROCESS/services.json` and an analysis report. It does not start task services. Reuse the same manifest for different Replay starting nodes.

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
- [ ] Resume: Restore agent context and a runnable environment, then create an independent branch from a historical node while preserving the original trace.
- [ ] Rewrite: Generate and actually execute replacement operations for a selected historical interval, validate its exit conditions and context handoff, and save a separate derived trace while preserving the original Record.
- [ ] Incremental storage management: Merge and split storage increments while retaining historical nodes.
- [ ] Portable backups and migration across machines.
- [ ] Btrfs storage pool expansion and reference reclamation.
- [ ] Full task-solving evaluation and validation across more tasks and environments.
