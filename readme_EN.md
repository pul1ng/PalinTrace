# PalinTrace

Execution trace recording, environment restoration, and historical replay for coding agents.

PalinTrace links an agent's model interactions, tool calls, execution results, context, and task environment state. The goal is to let users select any saved node, restore its associated environment, and replay recorded interactions from that point, keeping playback and environment state aligned with the same position in history.

- **Record · Trace recording (MVP implemented)**: Record model interactions, tool calls, and results together with the corresponding context, task files, and runtime configuration. These records provide the basis for inspecting history, restoring environments, and future replay.
- **Replay · Historical re-execution (MVP implemented)**: Restore a committed node and execute subsequent tools from a fixed history without model requests. Compare raw results and file states; stop on differences by default, or explicitly retain differences and continue.
- **Resume · Fork and continue (planned)**: Restore the agent context and task environment at any saved node, optionally provide new instructions, and continue on an independent trajectory while preserving the original history.

## Quickstart

Run the following commands from the project root. After installation and configuration, use the script to start Record. The resulting traces and snapshots are intended to serve as inputs for future Replay and Resume workflows.

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

### 3. Replay

Restore a committed baseline and execute subsequent historical tools without calling a model:

```bash
.venv/bin/palin replay "$PALIN_RUN" --from C20 --to C23
.venv/bin/palin replay "$PALIN_RUN" --from C2 --to C4 --continue-on-difference
```

The baseline becomes the new C0. `--to` is inclusive and defaults to the last committed node at startup; `--run` selects the source Run. `--output` selects a fresh directory, otherwise `outputs/replay/DATE_TASK/` contains `report.json` and an independent `record/`. The source trace remains unchanged.

Replay stops on the first difference by default. `--continue-on-difference` retains confirmed tool exit-code, raw-output and file differences while continuing remaining calls and historical steps using the actual workspace and context. Every completed step still receives the existing incremental file comparison. The option is saved in the new recording configuration.

Execution is reported as `completed / stopped / failed`, separately from validation `match / difference / incomplete`. A fully matching run exits `0`; any difference exits `2`, including a completed final step or a completed continuation. Operational faults take precedence and exit `1`, preserving earlier differences. The first difference remains recorded, and later steps carry `starting_divergent`. A later local incremental match cannot establish that the whole filesystem has converged.

Continuation preserves the strict comparison policy, including file mtime at integer-second precision and `.pyc` files. Corrupt materials, restore or snapshot failures, comparison exceptions, and uncertain tool results still stop execution. Replay does not rewrite commands, retry tools, repair files, or replace actual results with source snapshots. See the [command reference (Chinese)](docs/命令参考.md#replay).

### 4. Resume (not yet implemented)

## Getting Started

The following examples share a timestamp across stages and can be copied directly:

```bash
PALIN_STAMP=$(date +%y%m%d_%H%M%S)
PALIN_RAW="outputs/download/${PALIN_STAMP}_swebench_lite"
PALIN_TASKS="outputs/convert/${PALIN_STAMP}_swebench_lite"
PALIN_PRELOAD="outputs/preload/${PALIN_STAMP}_astropy__astropy-12907"
PALIN_RUN="outputs/record/${PALIN_STAMP}_astropy__astropy-12907"
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

The export does not include the Btrfs storage pool. Keep the recording directory, the base, and their associated storage pool for restoration verification and future Replay and Resume workflows. See the [command reference (Chinese)](docs/命令参考.md) for complete options.

### 7. record visualize: Generate a visualization

```bash
.venv/bin/palin record visualize "$PALIN_RUN"
```

The page is saved to `outputs/visual/<timestamp>_<task_id>.html` by default. Open it in a browser to inspect nodes, context, calls, timings, and Btrfs snapshot information. Use `--output FILE` to choose another location.

The playback controls at the bottom support pause, restart, speed selection, and looping, keeping the timeline and details in sync as records advance. This viewer plays back saved records; it does not run the agent or restore containers.

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

- [x] Replay MVP: Restore committed nodes and execute fixed historical tools without model requests, using strict comparisons and optional continuation after differences.
- [ ] Resume: Restore agent context and a runnable environment, then create an independent branch from a historical node while preserving the original trace.
- [ ] Incremental storage management: Merge and split storage increments while retaining historical nodes.
- [ ] Portable backups and migration across machines.
- [ ] Btrfs storage pool expansion and reference reclamation.
- [ ] Full task-solving evaluation and validation across more tasks and environments.
