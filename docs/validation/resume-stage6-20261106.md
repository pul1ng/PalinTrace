# Resume 第 6 步「结束并保留分支」最终验收

2026-11-06，真实模型从 Astropy 任务 `astropy__astropy-12907` 的源 C20 续跑，**完成 10 轮，在分支 C30 主动提交**。起点恢复了 42 条历史消息，服务 A、B 各启动一次。本次新增预算为 100 轮，结束时还剩 90 轮。

## 从运行报告找到分支

以下字段均取自本次会话的 `report.json`。源 C20 是分叉引用，分支 Store 的第一个节点是本次实际执行产生的 C21。

| 要查什么 | 本次结果 | 报告字段 |
| --- | --- | --- |
| 从哪里分叉 | 源 Run `a9261e45064b44029fa21e2cd2c3635e`，C20 的节点 ID 为 `c18d313c6bb04ac98b1ebba48f145ba3` | `node.run_id`、`node.index`、`node.node_id` |
| 分支身份 | 新 Run `f43f5f405f934f069dfb9edaa18a5424` | `recording.run_id` |
| 完成了多少轮 | 10 个完整节点，依次为 C21–C30 | `recording.complete_nodes` |
| 最后保存在哪里 | 分支 C30，节点 ID 为 `067ea35c7f664d9fb14d22bf14952fb7` | `recording.last_complete_node.index`、`recording.last_complete_node.node_id` |
| 为什么停止 | `submitted`，Run 状态为 `completed`，剩余预算为 90 | `recording.stop_reason`、`recording.run_status`、`recording.remaining_steps` |
| 调用了多少次 | 分支共发出 10 次模型请求（含首轮），执行 12 次工具调用 | `recording.total_model_requests`、`recording.tool_executions` |
| 费用是多少 | 本次总费用未知，`cost_known=false`、`cost=null` | `recording.cost_known`、`recording.cost` |

最后一次工具调用执行了 `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat patch.txt`，退出码为 0。Store 中唯一的 `RunEnd` 指向分支 C30。补丁将 `astropy/modeling/separable.py` 中的一行赋值改为写入右侧子矩阵 `right`。补丁正文在本次实验的 `tool-012.json` 中，SWE-bench 解题正确性需单独评测。

## 查看分支 C30 的文件快照

`RESUME_OUTPUT` 指包含 `report.json` 和 `record/` 的**本次会话目录**。查看自己的运行时，将它设为 Python 会话的 `session.output`。复核本例时，可在项目根目录执行以下命令，从本地归档取出会话。包内同一实验目录还包含 `acceptance.json` 和各次工具返回。

```bash
PALIN_ARCHIVE="../archives/261106_082856_stage-5-5/worktree-evidence.tar.gz"
PALIN_CASE="outputs/resume/261106_081238_C20_live100/real100"
PALIN_VIEW="$(mktemp -d)"
tar -xzf "$PALIN_ARCHIVE" -C "$PALIN_VIEW" "$PALIN_CASE/session"
RESUME_OUTPUT="$PALIN_VIEW/$PALIN_CASE/session"
```

在分支 Store 中选中 C30。

```bash
.venv/bin/palin record inspect "$RESUME_OUTPUT/record" --node C30 --json
```

查看 JSON 的 `selected_node`。其中 `run_id`、`node_id` 应与上表一致，`state_id` 为 `3550e7f33d73474c854b2a149ccdc336`，`state.manifest` 指向该节点的快照清单。要查父轨迹同名的 C30，需打开父轨迹的 Store。

`record/` 保存节点、上下文、调用和状态材料引用。快照文件由关联的 Btrfs 存储池保存，恢复时须保留该池和来源 Record。上面的命令用于定位 C30 的状态材料。独立文件恢复已在另一条四轮分支的 C21、C24，以及受控百轮分支的 C120 验证。

## 用其他结束情况交叉核对

下表的证据路径均相对于归档包内的 `outputs/resume/`。

| 结束情况 | 最后保留的状态 | 证据 |
| --- | --- | --- |
| 预算用尽 | 受控模型从 C20 执行 100 轮，C120 剩余预算为 0，Run 为 `paused`，原因为 `budget_exhausted`。删除执行容器后，独立恢复的 C120 包含全部 100 轮文件标记 | `261106_074235_stage55_review_fixes/budget-audit.json` 和同目录的 `controlled100-recovery/recovery.json` |
| 用户中断 | 第二轮工具被中断，最后完整节点为 C21。停止原因为 `interrupted`，Run 为 `cancelled`，第二轮保留一次结果未知的工具调用 | `261106_004858_multiturn/faults/faults.json` 的 `tool_interrupt` |
| 执行或存储故障 | 第二轮模型请求失败或快照发布前失败时，保留 C21。故障发生在 C22 发布之后时，C22 也保留。三种情况均报告 `failed` | 同一份 `faults.json` 的 `model`、`snapshot`、`after_publication` |
| 工具组提前提交 | 从源 C0 开始的实测中，提交工具排在第二个，两次工具都有真实返回，mini 只接纳提交工具的退出消息。Run 为 `incomplete`，本次产生 0 个完整节点，最后完整位置仍是源 C0 | `261105_224340_stage5_review_fixes/submit_last/report.json`。多步的 `incomplete_tool_group` 行为见[提交测试](../../tests/resume/test_run_steps.py#L133) |

查未完成的一轮时，在对应分支 Store 的 `record inspect` 输出中查看 `runs[].incomplete_steps` 和 `runs[].calls`。将上例的 `--node C30` 换成 `--execution` 和调用 ID，就能读取已保存的请求和返回。其他起点及恢复案例见[多步验收记录](../resume-multiturn-acceptance.md)。
