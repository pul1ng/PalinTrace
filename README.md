# PalinTrace

[English](readme_EN.md)

面向 Coding Agent 的轨迹录制、历史回放与分叉续跑工具。

PalinTrace 将模型交互、工具调用和结果与当时的 Agent 上下文、文件状态关联保存。选择一个已保存节点，可以恢复环境并沿历史执行后续操作，也可以让模型从恢复的会话开始新的分支。

- **Record · 轨迹录制（已实现 MVP）**：记录 Agent 的模型交互、工具调用与结果，关联当时的上下文、任务文件和运行配置，为历史查看、环境恢复与后续回放提供依据。
- **Replay · 历史重执行（已实现 MVP）**：从已提交的来源节点恢复环境，按固定历史真实执行后续工具，不调用模型、不需要模型凭据；保存真实结果与快照，确定的退出码不符或框架故障会停止执行。完整一致性校验另列为后续 Verify 工作。
- **Resume · 分叉续跑（已实现 MVP）** 通过 `palin resume` 从历史节点恢复环境和完整会话，让模型继续决策，在同一容器执行工具，并将每个完整步骤保存为独立分支上的新节点。

## 演示视频

https://github.com/user-attachments/assets/c46a93c8-3cd9-43a4-b142-0c557c4bb7b7

[下载演示视频（MP4，2.8 MB）](docs/media/palintrace-demo-zh.mp4?raw=true)

## Quickstart

以下命令在项目根目录执行。安装和配置完成后，通过脚本启动 Record，生成的轨迹与快照可用于 Replay 和 Resume。

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

### 5. 从历史节点分叉续跑（Resume）

使用来源录制 `$PALIN_RUN` 和前面配置的模型，让 Agent 从 C0 开始最多作出两轮新决策。来源也接受其中的 `record/` 目录；如果录制包含 C20，可以将 `--from C0` 改为 `--from C20`。

```bash
.venv/bin/palin resume "$PALIN_RUN" --from C0 \
  --max-steps 2 \
  --llm-config config/llm/llm.toml \
  --instruction "先检查当前工作目录" \
  --services-config "$PALIN_SERVICES"
```

没有服务清单时，省略 `--services-config`，程序会跳过准备，不自动推测服务。提供清单时，先完成文件、上下文及配置恢复和 R1 检查，再在同一容器准备起点服务。v2 清单中的选中启动命令各执行一次；旧配置显式指定的 readiness 检查继续生效。

`--from` 必填，支持 C 标签或完整节点 ID；来源不唯一时使用 `--run RUN_ID`。`--max-steps` 默认 `20`，必须为正整数，表示包含首次请求的**新增决策预算**。模型可以提前提交，不会被强制运行到预算上限。`--instruction` 默认不追加消息；提供时必须为非空白文本，在继承历史之后仅追加一条 `role=user` 消息。之后每次模型请求都使用完整累计上下文，包括新分支已接纳的工具结果。省略 `--llm-config` 时沿用[已有模型配置规则](docs/命令参考.md#模型配置)，本次模型由配置选择，不固定使用父轨迹的模型。

默认输出为 `outputs/resume/YYMMDD_HHMMSS_任务名/`，任务名取自选中的来源 Run；可用 `--output DIR` 指定来源录制之外、尚不存在的新目录。`report.json` 保存来源、模型请求、完整节点、停止及清理结果，`restore-report.json` 保存恢复证据，提供服务清单时生成 `preparation.json`，进入分支录制后生成 `record/`。早期失败只保留实际产生的材料。终端在清理结束后显示本次分支、最后完整节点及报告位置；无法确认的进度显示为 unknown。

Resume 与 Record 使用同一套原生 mini-SWE-agent 循环。模型仅返回文字或工具格式不正确时，将原生格式提示追加到完整会话后继续请求，纠正请求也计入新增预算；连续错误达到原生上限时停止（默认 3 次）。原始回答与纠正反馈均保留，模型需要通过原生提交动作结束任务。

从源 C20 分叉时，首个完整新节点为分支 C21，下一步为同一分支 C22；本次不新建 C0 或 C20，也不复制父轨迹的 C21/C22 结果。整组工具执行并接纳后才提交节点和文件快照；普通工具非零返回继续交给模型处理。格式纠正轮沿用 Record 的空工具组边界保存，不虚构工具执行。来源 C20 是外部引用，**须继续保留来源 Store、材料、镜像及关联 Btrfs 快照池**，仅复制分支目录不能得到自包含备份。

正常提交或预算耗尽返回 `0`；故障返回 `1`；参数解析错误返回 `2`；未完成工具组返回 `3`；无其他已知故障的取消返回 `130`。取消伴随录制、报告或清理故障时返回 `1`，并保留两类证据。预算耗尽是正常暂停，模型提交不代表答案已通过评测。完整参数及报告字段见[命令参考](docs/命令参考.md#resume)，Python 接口见[多轮能力验收](docs/resume-multiturn-acceptance.md)。

此前 Python 多轮验收中，真实模型从 C20 分叉，在第 10 轮主动提交并保存了 C30；其证据见[第 6 步验收记录](docs/validation/resume-stage6-20261106.md)。

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

分析已完成的 Record，提取后台服务的启动命令和启停区间，生成清单，供 Replay 和 Resume 在恢复起点时选择服务。

复制专用[模型配置模板](config/llm/preprocess_llm.toml.example)：

```bash
cp -n config/llm/preprocess_llm.toml.example config/llm/preprocess_llm.toml
```

在配置中填写 `model`、`base_url` 和 `api_key`，然后执行：

```bash
.venv/bin/palin preprocess "$PALIN_RUN" \
  --output "$PALIN_PREPROCESS"
```

此过程会调用模型，生成 `$PALIN_PREPROCESS/services.json` 和分析报告。同一份清单可供 Replay 和 Resume 在不同起点选择服务。

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
- [x] Resume 支持恢复已保存的节点、多步决策、分支快照和结束报告。
- [x] `palin resume` 命令行入口。
- [ ] Rewrite：为指定历史区间生成并实际执行替代流程，校验出口与上下文衔接，保存独立派生轨迹并保留原 Record。
- [ ] 增量整理：合并、拆分存储增量并保留历史节点。
- [ ] 便携备份与跨机器迁移。
- [ ] Btrfs 存储池扩容与引用回收。
- [ ] 完整解题评测及更多任务、环境的验证。
