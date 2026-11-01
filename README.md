# PalinTrace

[English](readme_EN.md)

面向 Coding Agent 的轨迹记录、环境恢复与历史回放工具。

PalinTrace 将 Agent 的模型交互、工具调用、执行结果、上下文和任务环境状态关联保存。项目目标是让使用者能够定位到轨迹中的任意已保存节点，恢复该节点对应的环境，并从该位置回放历史执行过程，使回放内容与环境状态始终对应同一个历史位置。

- **Record · 轨迹录制（已实现 MVP）**：记录 Agent 的模型交互、工具调用与结果，关联当时的上下文、任务文件和运行配置，为历史查看、环境恢复与后续回放提供依据。
- **Replay · 历史回放（计划中）**：从任意已保存节点恢复对应环境，按原有顺序回放交互与执行结果，使回放内容与环境状态同步对齐。回放使用已记录的历史，不重新调用模型或执行历史工具。
- **Resume · 分叉续跑（计划中）**：从任意已保存节点恢复 Agent 上下文与任务环境，可补充新的指令并继续运行，生成独立的新轨迹，保留原始历史。

## Quickstart

以下命令在项目根目录执行。安装和配置完成后，通过脚本启动 Record；生成的轨迹与快照将作为后续 Replay、Resume 的输入。

### 1. 安装依赖

启动 Docker 后，安装项目依赖并构建 Btrfs 工具与启动器：

```bash
uv sync --locked --extra mini --extra docker --extra swebench

docker build -f docker/btrfs/Dockerfile \
  -t palinreplay/btrfs-tools:0.1 docker/btrfs

docker build --platform linux/amd64 -f docker/btrfs/Dockerfile.bootstrap \
  -t palinreplay/btrfs-bootstrap:0.1 docker/btrfs
```

复制[模型配置模板](config/llm/llm.example.toml)：

```bash
cp -n config/llm/llm.example.toml config/llm/llm.toml
```

在 `config/llm/llm.toml` 中配置 `model`、`base_url` 和 `api_key`。

### 2. 启动 Record

```bash
bash scripts/quickstart_record.sh
```

该脚本将完成 Astropy 任务准备、C0 预载、录制、恢复校验、导出与可视化。

默认调用真实模型、最多运行两步。产物按类型保存在 `outputs/`，名称以 `YYMMDD_HHMMSS_` 开头；同一轮共用时间戳。脚本的 `--output DIR` 可更改产物根目录，其他参数见 `--help`。

### 3. Replay

从已提交节点恢复，并沿固定历史真实执行后续工具，不调用模型：

```bash
.venv/bin/palin replay "$PALIN_RUN" --from C20 --to C23
```

源 C20 成为新 C0，源 C21–C23 成为新 C1–C3。省略 `--to` 时固定启动时的末尾节点；`--run` 选择来源 Run，`--output` 指定新目录，默认写入 `outputs/replay/时间戳_任务ID/` 的 `report.json` 和独立 `record/`。工具原始输出、退出码及增量文件状态按固定规则比较，文件 mtime 严格按 `rootfs-files-v1` 的秒值比较。匹配返回 0，差异返回 2，执行/材料/清理失败返回 1；差异后停止并保留真实节点或未完成尾部。无自动重试或服务恢复，原轨迹保持不变。

### 4. Resume（待实现）

## Getting Started

以下示例用同一时间戳关联各阶段，便于直接复制执行：

```bash
PALIN_STAMP=$(date +%y%m%d_%H%M%S)
PALIN_RAW="outputs/download/${PALIN_STAMP}_swebench_lite"
PALIN_TASKS="outputs/convert/${PALIN_STAMP}_swebench_lite"
PALIN_PRELOAD="outputs/preload/${PALIN_STAMP}_astropy__astropy-12907"
PALIN_RUN="outputs/record/${PALIN_STAMP}_astropy__astropy-12907"
```

各命令也可省略 `--output`，自动在当前目录下的 `outputs/<类型>/` 生成带时间戳的路径，并在终端打印。显式指定 `--output` 时使用原样路径，保留已有结果。

### 1. download dataset：下载任务数据

下载 SWE-bench Lite 原始数据，保存任务内容、数据版本和来源信息。

```bash
.venv/bin/palin download dataset \
  --source swebench_lite \
  --output "$PALIN_RAW"
```

产物为原始数据文件和 `fetch.json`，供下一步转换使用。

### 2. convert：生成任务包

从原始数据中选择一个 Astropy 任务，转换为 PalinTrace 的任务格式，整理题面、环境配置、镜像引用和评测材料。

```bash
.venv/bin/palin convert \
  --input "$PALIN_RAW" \
  --instance-id astropy__astropy-12907 \
  --output "$PALIN_TASKS"
```

产物为包含 `manifest.json` 的任务包。转换阶段保存镜像引用，实际镜像在下一步准备。

### 3. download mirror：准备任务镜像

根据任务包拉取 Docker 镜像，核验平台与实际镜像身份，并显示下载进度。

```bash
.venv/bin/palin download mirror "$PALIN_TASKS"
```

成功后在任务包中生成 `images.json`，记录后续 preload 和 record 使用的固定镜像信息。

### 4. preload：生成 C0 基座

读取任务配置，创建临时容器，完成初始化，将初始文件状态冻结为可复用的只读 C0 基座。

```bash
.venv/bin/palin preload "$PALIN_TASKS" \
  --output "$PALIN_PRELOAD"
```

成功后返回 `preload_id` 和基座路径，供后续录制选择。默认保留镜像环境；额外初始化命令通过 `--init` 提供。

### 5. record run：启动录制

从 C0 创建独立任务容器，运行 Agent，记录模型交互、工具调用、上下文及各步文件状态。

```bash
.venv/bin/palin record run "$PALIN_TASKS" \
  --preload "$PALIN_PRELOAD" \
  --output "$PALIN_RUN" \
  --max-steps 2
```

`--preload` 选择上一阶段的基座，`--max-steps` 设置运行步数预算。录制复用 C0，后续 C1、C2 等文件状态通过 Btrfs 增量快照保存。产物包括 `report.json` 和 `record/`。

同一任务可以多次复用该基座：再次运行 `record run` 时，只需换一个新的 `--output` 目录，无需重复前面的准备步骤。

### 6. 查看、恢复校验与导出

查看运行状态、检查材料完整性，并查看一个已保存节点：

```bash
.venv/bin/palin record inspect "$PALIN_RUN" --check
.venv/bin/palin record inspect "$PALIN_RUN" --node C0
```

在独立容器中恢复 C0，完整比较文件后清理验证资源，生成校验报告：

```bash
.venv/bin/palin record verify-restore "$PALIN_RUN" \
  --node C0
```

可将 `C0` 换成实际已提交的 `C1`、`C2` 等节点。`inspect --check` 检查材料摘要及快照身份，`verify-restore` 执行完整文件恢复比较。

导出可阅读的轨迹：

```bash
.venv/bin/palin record export "$PALIN_RUN"
```

导出不包含 Btrfs 存储池。请保留录制目录、基座及其关联的存储池，供恢复校验和后续 Replay、Resume 使用。完整参数见[命令参考](docs/命令参考.md)。

### 7. record visualize：生成可视化

```bash
.venv/bin/palin record visualize "$PALIN_RUN"
```

页面默认保存到 `outputs/visual/时间戳_任务ID.html`，用浏览器打开即可查看节点、上下文、调用、耗时与 Btrfs 快照信息。`--output FILE` 可指定其他位置。

底部“自动播放”支持暂停、从头播放、倍速和循环，按记录顺序联动时间轴与详情。页面播放用于浏览已保存记录，不执行 Agent 或恢复容器。

## 项目结构

```text
src/palinreplay/      CLI、preload、record、存储与接入实现
tests/               按功能组织的自动化测试
scripts/             Record 快速启动脚本
examples/            使用示例与轨迹可视化
docker/btrfs/        工具与启动器镜像
config/llm/          模型配置模板
docs/                命令参考与当前验收证据
outputs/             按类型和时间戳分类的本地产物（Git 忽略）
```

## TODO

- [ ] Replay：在声明范围内重现已记录的输出和文件状态，不重新调用模型或重执行历史工具。
- [ ] Resume：恢复 Agent 上下文与可运行环境，从历史节点产生独立分支，保留原轨迹。
- [ ] 增量整理：合并、拆分存储增量并保留历史节点。
- [ ] 便携备份与跨机器迁移。
- [ ] Btrfs 存储池扩容与引用回收。
- [ ] 完整解题评测及更多任务、环境的验证。
