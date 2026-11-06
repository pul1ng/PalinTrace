# Resume CLI 验收记录

本轮验证 `palin resume` 能否连接现有恢复、服务准备、模型决策及分支录制能力。真实运行的代码版本为 `f4213b2`；该版本的 CLI 实现与 `45b85c1` 相同，后一个提交之后仅更新了使用文档。预算耗尽和模型提交都不表示任务答案通过评测。

## 来源与命令

复用已有 `outputs/record/261102_150636_service-overlap-40/record`，没有重新录制来源。它在另一个本地 checkout 中，以下通过环境变量引用；原始绝对位置保存在本地验收目录的 `preflight.json`。

| 项目 | 实际来源 |
|---|---|
| Run | `a9261e45064b44029fa21e2cd2c3635e` |
| C20 Node | `c18d313c6bb04ac98b1ebba48f145ba3` |
| State | `1379ebee3b81426494c547ce5676c1a7` |
| 完整历史 | 42 条消息，来源包含 C0–C40 |
| 服务 | 已有 v2 A/B/C 清单，C20 选中 A、B |
| 引导 | `下一步先检查 B 的日志`，一条新增 user 消息 |
| 新增预算 | `2`，包含首次模型请求 |

`PALIN_RUN` 指向上述录制目录，`PALIN_SERVICES` 指向保留的 v2 `services.json`。本次直接执行安装后的 CLI：

```bash
TERM=xterm-256color LITELLM_LOCAL_MODEL_COST_MAP=True \
  .venv/bin/palin resume "$PALIN_RUN" \
  --from C20 --run a9261e45064b44029fa21e2cd2c3635e \
  --max-steps 2 --llm-config config/llm/llm.toml \
  --instruction "下一步先检查 B 的日志" \
  --services-config "$PALIN_SERVICES" \
  --output outputs/resume/261106_112614_service-overlap-40_cli
```

私有配置复用本地现有网关设置，为模型名添加 LiteLLM 的 `openai/` provider 前缀；凭据保留在 Git 忽略的本地配置中。没有额外发送连接探测请求，也没有增加重试策略。

## 真实运行

本次通过实际 `.venv/bin/palin resume` 命令完成两轮真实模型续跑，CLI 返回 `0`，停止原因为 `budget_exhausted`。

| 项目 | 实测结果 |
|---|---|
| 模型配置 | `openai/commandcode/deepseek/deepseek-v4.1-flash:max[1m]`，当前配置选择，独立于父轨迹 |
| 服务清单 SHA-256 | `241a072f9c145ab28bf7c455b8a9e306034ea397964a9eba36ef8fe68437887e` |
| 恢复及 R1 | 通过，继承 42 条历史消息 |
| 服务准备 | A、B 启动各一次；C 未提前启动；没有额外 readiness 命令 |
| 分支 Run | `693974794966475a96d0aaa95272a6e3` |
| 新增完整节点 | 2 个：C21、C22；没有本地 C0 或 C20 |
| C21 | Node `fd1665c003cd4a219235a91696b7e214`，State `1aabf89d8c334b28a7d327c6fa053d2a` |
| C22 | Node `c4f2071eff5847c482c96221710d2025`，State `4364577cfef74ca58b072c5356951341` |
| 本次模型请求 | 2 次，输入消息数依次为 43、46 |
| 完整节点消息数 | 46、49；instruction 仅追加一次 |
| 工具执行 | 4 次，与分支调用及报告一致 |
| 最后完整位置 | 分支 C22 |
| 同一容器 | 两个新节点的 runtime 均指向本次恢复会话的执行容器 |
| 清理 | 原执行容器、辅助容器、临时卷均已删除；池工作区回到基线 |
| 来源 | 录制文件摘要、全部来源节点、C20 只读快照及清单均未改变 |
| 密钥检查 | 终端日志、报告、查看结果及本次分支材料共 58 个文件未发现配置密钥 |

逐节点读取模型请求、已接纳工具结果、上下文、输入位置和文件状态，并执行 Store 完整性检查。首次请求的历史前缀与源 C20 一致，下一条为新增 user 引导；第二次请求与本分支 C21 的完整上下文一致。原执行容器已确认不存在后，使用现有入口查看并恢复本次 C22：

```bash
PALIN_RESUME=outputs/resume/261106_112614_service-overlap-40_cli
PALIN_CLI_CHECK=outputs/validation/261106_112614_resume_cli
.venv/bin/palin record inspect "$PALIN_RESUME" \
  --run 693974794966475a96d0aaa95272a6e3 --node C21 --json
.venv/bin/palin record inspect "$PALIN_RESUME" \
  --run 693974794966475a96d0aaa95272a6e3 --node C22 --json
.venv/bin/palin record verify-restore "$PALIN_RESUME" \
  --run 693974794966475a96d0aaa95272a6e3 --node C22 \
  --report "$PALIN_CLI_CHECK/verify-C22.json"
```

独立恢复命令返回 `0`，报告为 `verified`。新恢复容器与原执行容器不同；`rootfs-files-v1` 全文件比较覆盖 **72,506 个路径，差异 0**，硬链接关系一致。恢复后的临时容器、辅助容器、卷和工作区均已清理，再次核对来源仍保持不变。

成功输出为 `outputs/resume/261106_112614_service-overlap-40_cli/`，其中 `report.json` 为会话报告，`record/` 为分支 Store。原始日志及审计位于 `outputs/validation/261106_112614_resume_cli/`：

- `cli.log`、`cli-exit-code.txt`：实际终端结果及退出码。
- `preflight.json`、`postflight.json`、`acceptance.json`：来源基线、上下文与调用核对、独立恢复及资源清理结论。
- `inspect-C21.json`、`inspect-C22.json`：现有查看命令保存的节点与调用证据。
- `verify-C22.json`、`verify-C22.log`、`verify-C22-exit-code.txt`：独立恢复的完整报告、日志及退出码。

原始材料留在本地 Git 忽略目录；须保留来源 Store、材料、镜像及关联快照池才能复核，不把分支目录视为自包含备份。

## 失败路径证据

第一次直接调用使用另一份既有配置的 `deepseek-v4-flash`。C20 恢复、R1、A/B 准备均已通过，模型端点随后返回 `404 unknown model`。实际请求为 1 次、工具执行为 0 次、新增完整节点为 0，CLI 返回 `1`，最后完整位置显示源 C20，未创建分支 Store。

- 失败输出：`outputs/resume/261106_112051_service-overlap-40_cli/`。
- 原始日志及审计：`outputs/validation/261106_112051_resume_cli/` 中的 `cli.log`、`preflight.json`、`failed-attempt.json`。
- 已独立确认清理完成，来源文件、节点及只读快照保持不变；日志和报告中未出现本次配置密钥。

这次零节点运行仅作为失败路径证据，不能替代成功保存并恢复新节点的验收。

## 命令与受控验证

截至文档提交 `f4213b2`，新增 CLI 测试共 74 项通过。覆盖解析、配置、来源选择、输出隔离、恢复与准备顺序、首次决策、两轮累计上下文、预算 1/2/100、提前提交、普通工具非零返回、未完成尾部、失败与取消优先级、发布事实、清理后摘要及未知进度。关键受控测试执行真实 Resume 编排和临时 Store，替换模型、容器及快照边界。

中英文 README 和命令参考中的 3 条 Resume 命令已通过实际解析，11 个新增本地链接与锚点已核对。各项对应的局部回归及 Ruff、差异检查已执行；整组最终回归结果将在第 13 项完成后补入。

## 历史能力依据

既有 100 轮预算、多个分叉起点及真实模型 C20→C30 自然提交的结果继续作为核心能力依据，见 [多轮 Resume 验收](resume-multiturn-acceptance.md) 和 [此前第 6 步验收](validation/resume-stage6-20261106.md)。本轮没有重新执行这些长实验，也没有把历史结果计入本次 CLI 的请求或节点统计。
