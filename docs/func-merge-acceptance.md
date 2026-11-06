# 公共组件整理验收记录（2026-11-06）

本轮在 `palin-func-merge` worktree 完成 Record、Replay、Resume 公共组件整理。整理前基准为 `48c7c20`，功能实现版本为 `d3e5608`。依据 `docs/dev/2611061723_func_merge/{background,metatask,plan,check}.md`；最初提供的 `261106_func_merge` 背景和清单路径已不存在，使用同一开发文档目录中的对应文件。

证据目录为 [outputs/func-merge-acceptance/261106_refactor](../outputs/func-merge-acceptance/261106_refactor/)。该目录不纳入 Git，须与本记录一起归档；包含本次测试日志、临时验收驱动、运行产物与恢复报告。没有修改来源 Store，也没有提交模型凭据或私有配置。

## 分项实现与验证

每项实现、直接相关测试及静态检查完成后，才进入下一项。下表数字是本次实际结果，各次测试集合存在交集，不作累计总数。各项命令、环境变量、版本和计数另见 [validation-commands.json](../outputs/func-merge-acceptance/261106_refactor/validation-commands.json)。

| Meta Task | 本地 commit | 本次相关 pytest 结果 | 日志 |
| --- | --- | --- | --- |
| 1 工具结果编码 | `0d8ec65` | 50 passed，1 warning | [task01](../outputs/func-merge-acceptance/261106_refactor/task01-pytest.log) |
| 2 工具适配器创建 | `865287d` | 132 passed，1 warning | [task02](../outputs/func-merge-acceptance/261106_refactor/task02-pytest.log) |
| 3 工具消息检查与配对 | `b55cfb0` | 79 passed，1 warning | [task03](../outputs/func-merge-acceptance/261106_refactor/task03-pytest.log) |
| 4 进度初始化与终端选择 | `c57d651` | 63 passed | [task04](../outputs/func-merge-acceptance/261106_refactor/task04-pytest.log) |
| 5 进度通知与清理 | `853d964` | 220 passed | [task05](../outputs/func-merge-acceptance/261106_refactor/task05-pytest.log) |
| 6 公共恢复执行器 | `88b0888` | 150 passed | [task06](../outputs/func-merge-acceptance/261106_refactor/task06-pytest.log) |
| 7 公共 R1 校验 | `2ff7aa6` | 163 passed | [task07](../outputs/func-merge-acceptance/261106_refactor/task07-pytest.log) |
| 8 公共服务准备 | `c4dd992` | 210 passed | [task08](../outputs/func-merge-acceptance/261106_refactor/task08-pytest.log) |
| 9 按来源节点选择服务 | `708fcb4` | 275 passed | [task09](../outputs/func-merge-acceptance/261106_refactor/task09-pytest.log) |
| 10 公共 Store 路径识别 | `d3e5608` | 85 passed | [task10](../outputs/func-merge-acceptance/261106_refactor/task10-pytest.log) |

上述测试没有失败或跳过。前三项各有一条 LiteLLM `_service_logger.py` 的 `DeprecationWarning: There is no current event loop`。各项最终 Ruff 与 diff 检查通过；迁移时发现的导入及兼容导出排序问题已在对应任务提交前修正。

公共工具编码沿用 `BorrowedEnvironment.output()`，退出状态由调用方决定，Replay 对非整数仍保存 `unknown` 后进入失败路径。消息配对在整个批次校验通过后才交给原 `adopt_messages()`。进度计数、原生格式纠正与提交、Replay 退出码停止、已保存节点和未完成尾部等既有行为由相关测试复核。

恢复执行器、R1 和服务准备实现归入 `restoration/`；旧导入保留薄兼容入口。服务解析直接接收来源 Node 与所需范围，完整分析范围摘要、来源 Run、全部证据和半开区间规则继续有效。公共来源读取只迁移元数据选择及原 provenance 编码，没有引入新的执行调度。分项测试补足了独立摘要格式断言、A/B/C 边界表、未选中服务的证据校验以及 Replay 范围与 Resume 起点的区别。

## 全量回归

本次结果：**1964 passed、3 skipped、1 warning，0 failed**，耗时 155.58 秒。[完整 pytest 日志](../outputs/func-merge-acceptance/261106_refactor/full-pytest.log)、[Ruff 日志](../outputs/func-merge-acceptance/261106_refactor/full-ruff.log)、[diff 检查日志](../outputs/func-merge-acceptance/261106_refactor/full-diff-check.log)。Ruff 和 diff 检查通过。

3 个 skip 全部来自 `tests/record/test_replay_verification_scope.py` 的 `history="local"` 参数：该分支没有外部来源，1 个直接来源检查和 2 个损坏外部来源的 inspect/export 检查不适用，提示为 `requires an external source`。同一测试文件中带外部来源的共享池及独立池参数实际执行通过。这些 skip 不是 Docker 或依赖缺失，未将其计为通过。唯一 warning 为前述 LiteLLM 事件循环弃用警告。

从项目根目录执行，完整输出见证据目录：

```bash
.venv/bin/ruff check src tests
TERM=xterm-256color LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest -q -ra
git diff --check
```

## 受控真实容器验收

三个场景全部通过，实际驱动总耗时 174.03 秒。使用真实 Docker、已有 Astropy preload 和 Btrfs 池；Record、Resume 的模型响应固定供给，真实模型 HTTP 请求均为 0。Replay 在禁止模型传输的 guard 下运行完整 CLI 入口，模型传输调用次数和报告中的实际模型请求均为 0。

| 流程 | 产物目录（相对证据目录） | 模型与实际工具执行 | 已保存节点 | 停止及清理 |
| --- | --- | --- | --- | --- |
| Record | `record/` | 2 次固定响应，2 次工具执行 | C0、C1、C2 | `LimitsExceeded`，RunEnd 为 `paused`，清理确认 |
| Replay | `replay/` | 模型调用 0，2 次历史工具执行 | 本次独立 Run 的 C21、C22 | 所选范围完成，CLI 返回 0，清理确认 |
| Resume | `resume/session/` | 2 次新请求均供给固定响应，2 次工具执行、2 次快照 | 本次独立分支的 C21、C22 | `budget_exhausted`，RunEnd 为 `paused`，清理确认 |

Record 两轮工具分别写入并读取受控文件，原始结果为 `step-1\n` 与 `step-1\nstep-2\n`，结果接纳后提交节点。原容器删除后，分别恢复 C1/C2 并读取，得到对应的一行/两行内容。

Replay 的实际顺序为 `R1 passed → start A → start B → tool → tool`，五个边界记录的容器 ID 相同。v2 的 A/B 启动各一次，没有 readiness 请求，C 未启动；历史动作也实际断言了 A/B 可用、C 不可用。独立恢复 C21 时结果 JSON 为 `{ "step": 21, "services": ["A", "B"], "total": 21 }`；恢复 C22 后输入文件总和为 42。本次 Store 仅有 C21/C22，起点 C20 为外部引用，没有新增本地 C0/C20。

Resume 使用同一来源 C20 与清单，A/B 各启动一次，恢复、准备和两轮执行位于同一容器。来源前缀 42 条消息，追加 user 指令一次；两轮请求为 43/45 条消息，节点会话为 45/47 条。既有验收器逐轮比较请求、模型及工具接纳记录和文件变化；原容器删除后，C21/C22 独立恢复通过，见 [恢复汇总](../outputs/func-merge-acceptance/261106_refactor/resume-recovery/recovery.json)。这些供给次数和 Agent 工具计数不包含另行记录的验收探针命令。

三条产物的 `palin record inspect --check` 均返回 0、`Integrity: passed`。另外通过实际 CLI 对 Record C2 与 Replay C22 执行 `verify-restore`：分别完整比较 72,478 和 72,507 个路径，均为 `equal=true`、差异路径 0、hardlinks 一致，恢复报告确认清理完成。完整比较只发生在显式验收和原有 R1 路径，未加入产品逐步执行。

来源目录逐文件摘要、完整节点列表及服务清单摘要在验收前后相同。C20 快照保持只读及原 UUID；池内临时 workspaces 前后均为空，辅助容器已移除、loop 已释放、mount 记录已清理。新轨迹与只读快照保留，读取和独立恢复均有本次证据。

本轮不调用真实模型、不重新生成 40 步来源，也不重跑 100 轮实验；模型格式纠正、提前提交、非零/未知退出、取消与故障路径由上述本次回归覆盖。适用验收项已完成，没有阻塞或新增产品需求；3 个不适用参数化 skip 如上单独记录。

使用既有 `run_record(..., completion=...)` 固定响应边界、Replay CLI 入口和 `examples/resume_multiturn_validation.py`，没有新增产品参数或重建 40 步来源。多轮验收脚本的服务启动观测已从旧 Replay 模块对齐到公共实现，以保证启动次数统计对应实际执行。

可复核入口与证据：

- [本次临时驱动](../outputs/func-merge-acceptance/261106_refactor/runtime_acceptance.py)、[运行日志](../outputs/func-merge-acceptance/261106_refactor/runtime.log)、[结构化汇总](../outputs/func-merge-acceptance/261106_refactor/runtime-summary.json)。
- 来源为 `../PalinTrace/outputs/record/261102_150636_service-overlap-40/record`，Run `a9261e45064b44029fa21e2cd2c3635e`，起点 C20，前缀 42 条消息。
- 服务清单为 `../archives/261104_141057_palin-preprocess-services/outputs/validation/261104_123158_single_pass_preprocess_replay/preprocess/services.json`。
- 任务包为 `../../PalinReplay/data/btrfs-ten-direct-20261030-223433/tasks`；preload 为同一数据目录下的 `results/astropy__astropy-12907/preload`。
- 三条流程的轨迹、会话与恢复报告分别保存在证据目录的 `record/`、`replay/`、`resume/` 和 `resume-recovery/`；完整文件比较报告与 CLI 读取日志位于该证据目录顶层。

复现本次驱动的命令：

```bash
TERM=xterm-256color LITELLM_LOCAL_MODEL_COST_MAP=True \
  .venv/bin/python -u outputs/func-merge-acceptance/261106_refactor/runtime_acceptance.py
```

驱动输出采用不覆盖规则；重新执行应使用新的独立输出目录，并保留其引用的来源材料与快照池。
