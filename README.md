# PalinTrace

[English](readme_EN.md)

面向 Coding Agent 的轨迹记录、环境恢复与历史回放工具。

PalinTrace 将 Agent 的模型交互、工具调用、执行结果、上下文和任务环境状态关联保存。项目目标是让使用者能够定位到轨迹中的任意已保存节点，恢复该节点对应的环境，并从该位置回放历史执行过程，使回放内容与环境状态始终对应同一个历史位置。

- **Record · 轨迹录制（已实现 MVP）**：记录 Agent 的模型交互、工具调用与结果，关联当时的上下文、任务文件和运行配置，为历史查看、环境恢复与后续回放提供依据。
- **Replay · 历史重执行（已实现 MVP）**：从已提交的来源节点恢复环境，按固定历史真实执行后续工具，不调用模型、不需要模型凭据；保存真实结果与快照，确定的退出码不符或框架故障会停止执行。完整一致性校验另列为后续 Verify 工作。
- **Resume · 分叉续跑（分阶段实现）**：可恢复已保存节点、校验上下文与可写环境、准备所需服务，再取得一次新决策并将其整组工具录制为独立分支节点。

## 演示视频

https://github.com/user-attachments/assets/c46a93c8-3cd9-43a4-b142-0c557c4bb7b7

[下载演示视频（MP4，2.8 MB）](docs/media/palintrace-demo-zh.mp4?raw=true)

## Quickstart

以下命令在项目根目录执行。安装和配置完成后，通过脚本启动 Record；生成的轨迹与快照可用于 Replay，并为后续 Resume 提供输入。

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

### 3. Preprocess（生成服务清单）

分析已有 Record，提取后台服务的启动命令和启停区间，生成 `services.json`，供 Replay 及 Resume 恢复环境时使用。此过程会调用模型。

复制[预处理模型配置模板](config/llm/preprocess_llm.toml.example)：

```bash
cp -n config/llm/preprocess_llm.toml.example config/llm/preprocess_llm.toml
```

在配置中填写 `model`、`base_url` 和 `api_key`，将 `PALIN_RUN` 设为上一步的录制结果目录：

```bash
PALIN_RUN="/path/to/record"
.venv/bin/palin preprocess "$PALIN_RUN"
```

清单与分析报告默认保存到 `outputs/preprocess/YYMMDD_HHMMSS_任务名/`，终端会显示输出路径。

### 4. Replay

从已保存节点恢复环境，沿原轨迹真实执行后续操作，保存新的结果与快照，保留原始录制，不调用模型。将 `PALIN_SERVICES` 设为上一步生成的 `services.json` 路径：

```bash
PALIN_SERVICES="/path/to/services.json"
.venv/bin/palin replay "$PALIN_RUN" --from C0 \
  --services-config "$PALIN_SERVICES"
```

示例从 C0 执行到录制末尾，可用 `--from`、`--to` 选择区间。结果默认保存到 `outputs/replay/YYMMDD_HHMMSS_任务名/`；退出码与历史不一致时停止并保留结果。完整参数和恢复范围见[命令参考](docs/命令参考.md#replay)。

### 5. Resume：恢复起点并录制一轮分支

Python 入口可恢复原始对话前缀和可写环境，通过 R1 文件校验并准备可选服务，然后在同一容器执行一份新决策、保存分支节点：

```python
from palinreplay.resume import (
    select_resume_start, restore_resume_start, decide_resume_start, record_resume_step,
)

start = select_resume_start("outputs/record/来源录制", "C20", model="openai/M", max_steps=1)
with restore_resume_start(start, services_config="outputs/preprocess/来源清单/services.json") as session:
    decision = decide_resume_start(session, llm_config="config/llm/llm.toml")
    node = record_resume_step(session, decision)
    if node is not None:
        print(node.run_id, node.node_id, node.index)  # 独立分支 C21
```

`session` 提供 `start`、`materials`、`restored`、`executor`、`prefix`、`context`、
`input_position`、`output`、`report` 和 `report_path`。原始运行配置通过
`session.materials.config` 访问；本次模型与新增预算留在 `start`，历史计数保持原值。
默认输出为当前目录的 `outputs/resume/YYMMDD_HHMMSS_C20-restore/`，也可传入来源录制目录之外的
新 `output` 目录。`report.json` 分别记录恢复、服务准备、调用方错误及清理结果；`restore-report.json`
保留底层资源与 R1 证据，提供清单时的 `preparation.json` 保存选中服务的实际命令、返回和耗时。
退出上下文时清理执行器和恢复资源，自行创建的 Docker 客户端也会关闭；
传入的 `client` 仍由调用方管理。

`services_config` 可省略，此时跳过准备；有效清单未选中服务时，记录零项准备完成。
清单来源、摘要、证据及起点范围在创建任务容器前校验。R1 通过后，v2 清单中的选中服务按顺序
各执行一次启动命令，不添加 readiness 或业务请求；旧配置显式提供的 readiness 继续兼容。
准备失败不交付会话，保留已完成的恢复和准备证据并清理资源。调用方之后抛错不会改写先前结论。

`decide_resume_start()` 取得一次回答，可通过 `instruction=` 在来源前缀之后追加引导。
`record_resume_step()` 消费这份已保存的回答。即使决策预算已用完，本轮工具也会继续执行，新增模型请求数为零。
两者须使用同一个打开的会话，修改决策、重复消费或关闭后调用会在执行前被拒绝。
工具逐条保存实际输出和退出码，全部接纳后才在 `session.output / "record"` 提交一个新节点。
mini 收到任务提交标记时，会接纳提交工具的 `exit` 消息。单工具提交会保存节点。多工具组因此提前结束时，
函数返回 `None`，录制和会话报告记为 `incomplete`，已执行工具的真实返回保存在 Store 中。
普通非零退出照实接纳；中断和录制故障保留已发生的调用，并在 `report.json.recording` 中报告实际完整节点数。
来源前缀仍由 `session.context` 保留，分支更新后的消息与输入位置保存在新节点中。
当前入口完成一轮；自动请求下一轮决策的循环留待后续。验收与重跑方式见[第五步验收记录](docs/resume-stage5-acceptance.md)。

R1 范围是 `rootfs-files-v1`，完整排除项、属性和限制保存在报告中；它不恢复进程内存，后台服务由清单重新启动。
明确包含 `exit` 消息的终止前缀会被拒绝，父 Run 已结束不妨碍恢复中间节点。

## Getting Started

以下示例用同一时间戳关联各阶段，便于直接复制执行：

```bash
PALIN_STAMP=$(date +%y%m%d_%H%M%S)
PALIN_RAW="outputs/download/${PALIN_STAMP}_swebench_lite"
PALIN_TASKS="outputs/convert/${PALIN_STAMP}_swebench_lite"
PALIN_PRELOAD="outputs/preload/${PALIN_STAMP}_astropy__astropy-12907"
PALIN_RUN="outputs/record/${PALIN_STAMP}_astropy__astropy-12907"
PALIN_PREPROCESS="outputs/preprocess/${PALIN_STAMP}_astropy__astropy-12907"
PALIN_REPLAY="outputs/replay/${PALIN_STAMP}_astropy__astropy-12907"
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

导出不包含 Btrfs 存储池。请保留录制目录、基座及其关联的存储池，供恢复校验和后续 Replay、Resume 使用。若导出的是 Replay 结果，还须保留它引用的原始来源 Store、材料及快照；导出会声明外部依赖，不会将它们打包进来。完整参数见[命令参考](docs/命令参考.md)。

### 7. record visualize：生成可视化

```bash
.venv/bin/palin record visualize "$PALIN_RUN"
```

页面默认保存到 `outputs/visual/时间戳_任务ID.html`，用浏览器打开即可查看节点、上下文、调用、耗时与 Btrfs 快照信息。`--output FILE` 可指定其他位置。

底部“自动播放”支持暂停、从头播放、倍速和循环，按记录顺序联动时间轴与详情。页面播放用于浏览已保存记录，不执行 Agent 或恢复容器。

### 8. preprocess：生成服务清单

分析已完成的 Record，提取后台服务的启动命令和启停区间，生成清单，供 Replay 及后续 Resume 恢复环境时使用。

复制专用[模型配置模板](config/llm/preprocess_llm.toml.example)：

```bash
cp -n config/llm/preprocess_llm.toml.example config/llm/preprocess_llm.toml
```

在配置中填写 `model`、`base_url` 和 `api_key`，然后执行：

```bash
.venv/bin/palin preprocess "$PALIN_RUN" \
  --output "$PALIN_PREPROCESS"
```

此过程会调用模型，生成 `$PALIN_PREPROCESS/services.json` 和分析报告，不启动任务服务。同一份清单可供不同 Replay 起点复用。

### 9. replay：恢复并重执行历史

使用原录制和上一步的服务清单，从 C0 恢复并执行到录制末尾：

```bash
.venv/bin/palin replay "$PALIN_RUN" \
  --from C0 \
  --services-config "$PALIN_PREPROCESS/services.json" \
  --output "$PALIN_REPLAY"
```

用 `--from`、`--to` 可选择其他已保存区间；服务清单按起点选择需要准备的服务。Replay 不调用模型，将本次真实结果和快照保存到 `$PALIN_REPLAY`，原录制保持不变。退出码与历史不一致时停止，并保留已执行结果。

查看本次结果和材料完整性：

```bash
.venv/bin/palin record inspect "$PALIN_REPLAY" --check
```

本次只保存实际执行产生的节点，从源 C0 开始时首个新节点为 C1。完整参数与恢复范围见[命令参考](docs/命令参考.md#replay)。

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

- [x] Replay MVP：从已提交的来源节点恢复，通过 R1 与显式服务准备后沿固定历史真实执行工具，不调用模型；引用源起点，只保存实际完成步骤，按确定的历史退出码控制继续或停止并保留真实证据。
- [ ] Verify：独立检查 Replay 实际结果与固定来源的一致性；当前尚无已实现的 Verify 命令。
- [ ] Resume：恢复 Agent 上下文与可运行环境，从历史节点产生独立分支，保留原轨迹。
- [ ] Rewrite：为指定历史区间生成并实际执行替代流程，校验出口与上下文衔接，保存独立派生轨迹并保留原 Record。
- [ ] 增量整理：合并、拆分存储增量并保留历史节点。
- [ ] 便携备份与跨机器迁移。
- [ ] Btrfs 存储池扩容与引用回收。
- [ ] 完整解题评测及更多任务、环境的验证。
