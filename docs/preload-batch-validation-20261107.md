# 批量 Preload 真实验收（2026-11-07）

在 `palin-preload-batch` 工作树、代码版本 `58c5c631e4b60ef3cc4ad42e941cb1122ae040e2` 上，
对 SWE-bench Verified 清单前 10 题执行一次不带 `--task-id` 的真实批量构建。
命令退出码为 0，总耗时 665.493 秒；索引中的 10 题均成功，顺序与清单一致。

```bash
PYTHONUNBUFFERED=1 .venv/bin/palin preload outputs/validation/261107_164835_preload_batch/tasks --output outputs/preload/261107_164835_swebench_verified_10
```

- 任务准备：从已有 500 题转换产物提取前 10 题，使用现有镜像准备与绑定校验，并只跳过已缓存镜像的拉取；原包清单和所选材料、本轮 10 题包的全部文件在构建前后的摘要一致。来源（相对于主项目根目录）：`outputs/convert/261107_130137_swebench_verified`。
- 回归：`tests/preload`、`tests/cli`、Record 运行和兼容测试及 Btrfs 存储测试共 407 项通过（36.26s）。Ruff 与 `git diff --check e718134 HEAD` 通过。
- 每份 C0 使用现有读取和选择能力核对元数据摘要、任务输入及镜像绑定、初始化摘要与启动配置；通过 Btrfs 池校验快照 UUID 和只读属性，并核对保存的导入清单摘要。构建记录均确认导入时的文件清单往返比较通过。
- 不同题的 preload ID、池、池 UUID 和快照 UUID 均不同。逐题确认本轮源容器及池管理容器已移除，并对照构建前后的受管容器清单；持久池保留，交叉选择另一题基座会被拒绝。
- 首次独立核验已通过 10 份逐题检查，但 Docker 事件历史不足导致资源事件汇总断言失败；已保留首次失败日志。改用构建前后的受管容器清单后，完整复验退出 0，基座构建没有重跑。
- 按 Preload 调用路径核对，构建不调用模型客户端，模型调用为 0；本次没有独立的网络级请求计数。未另外做全量文件恢复比较。
- 完整证据在 `outputs/validation/261107_164835_preload_batch/`，真实基座在 `outputs/preload/261107_164835_swebench_verified_10/`。这些本机生成物未提交 Git。`verification.json` 逐题保存摘要、镜像及资源核对结果。

| 任务 | preload_id | 相对路径 | 耗时（秒） | 存储池 | 快照 UUID | 材料与清理 |
|---|---|---|---:|---|---|---|
| `astropy__astropy-12907` | `preload:sha256:00f3627cdb4f67678d8967984ff7e6718b580f736f72b66346cca6cd02ba9cb2` | `astropy__astropy-12907/` | 67.542 | `palinreplay-btrfs-cdcaa1b65acb4eb6990b749f51ca51ec` | `27517b1e-f625-314b-8297-137952405a1d` | 通过 |
| `astropy__astropy-13033` | `preload:sha256:aa24501ebdcd2925255416a80bf99bb406fedd561c6e8d834527282e15751f8b` | `astropy__astropy-13033/` | 65.934 | `palinreplay-btrfs-9850a68bca72417bb016c9cab093eb75` | `44e0389d-7990-e44b-b1a2-cacf9f61f321` | 通过 |
| `astropy__astropy-13236` | `preload:sha256:d8a0343e5b78b56c5ef8b55004d5090ff748e5d39ddc4e942ef6b987d0091af5` | `astropy__astropy-13236/` | 63.700 | `palinreplay-btrfs-6351b31d82754d23b1b85cb2c9a4ad8b` | `c5ba94fc-fc1b-3248-9acc-884c08a4a0b8` | 通过 |
| `astropy__astropy-13398` | `preload:sha256:269f11581bce6cc46fa3023bc7e4dc5be4f651fbad8c60f5dfc84c76a4b5bd74` | `astropy__astropy-13398/` | 64.682 | `palinreplay-btrfs-ca238b10baf64a98b104adc9c20f0b93` | `1d548013-0961-fc45-9d88-ca52ed494e82` | 通过 |
| `astropy__astropy-13453` | `preload:sha256:77bc9e05dbab78579cd72dda3325bd77832582f3834bc5ca28902698f2d8ef43` | `astropy__astropy-13453/` | 69.557 | `palinreplay-btrfs-f744a9a9f6b241dc98104a43d85f882a` | `af1fa53e-c37a-3e41-b327-8ba89948b734` | 通过 |
| `astropy__astropy-13579` | `preload:sha256:6f897f355a5e41a23d2f7eaa78758cce795d92d89cf3ee18459fb99c14b37189` | `astropy__astropy-13579/` | 62.668 | `palinreplay-btrfs-6c06d0f5b48a48e288c141c38812efba` | `443a42cc-170e-1d48-a399-9a7b541cd05f` | 通过 |
| `astropy__astropy-13977` | `preload:sha256:11684bc03dd04fad9f96e162e2209433a00f427a7118daddfee002401275f0c6` | `astropy__astropy-13977/` | 66.177 | `palinreplay-btrfs-8862df91c2ec4bab9c7125c8e931a824` | `bcf7365a-f78e-6b49-b98e-4fc538974dd6` | 通过 |
| `astropy__astropy-14096` | `preload:sha256:91638dc228b57cece36e6eded768e3877313f5120dec1121577fe3edba4437f8` | `astropy__astropy-14096/` | 66.397 | `palinreplay-btrfs-e6034bb0ec0642049d8074955039774e` | `de5469fc-646f-904c-9214-4e3e10220b9e` | 通过 |
| `astropy__astropy-14182` | `preload:sha256:8af7ba293a8c7a51c190fad3a98b34309039cc5a0930e28127cb8cf9d0cfe67a` | `astropy__astropy-14182/` | 72.004 | `palinreplay-btrfs-d43f2a8a8ebc473f8f1e4a8adc6dba10` | `eac30516-3451-b846-83b0-295d006271d2` | 通过 |
| `astropy__astropy-14309` | `preload:sha256:6e1eef3a31d8e752cb43b69b8043069e934831aac79604a47427a615e5b7e6cf` | `astropy__astropy-14309/` | 65.973 | `palinreplay-btrfs-51cf5fce0c3e44f7a3abdaa34233ab3d` | `d219ae44-5e03-f543-b015-cc627464eda0` | 通过 |

验收材料 SHA-256：

- `build.log`：`8cb7cab5368d029fe1e5a7ec5e4fe50dab02f9f5f73bcb9701e872f19cb3a5bb`
- `verification.log`：`38d96fded655e4424bc55c0dbd4a8357a4ec081db0847da9fd49094a996f248c`
- `verification-attempt-1.log`：`9439c61bae8b0a3a17a54090e06a723c3add963dcfdf16227e30cacebd0ea43c`
- `regression.log`：`6481219216cea37a86b3fca44e1a8eb1ae2073ecbec273b5edf4e919dded3a6e`
- `before.json`：`8d53c6af1fb08b1c00f221543bd1344b3cb94524ce0707e7d7f97ed1cc495b56`
- `verification.json`：`bc5d7bdb0503b367686d8b450bcf215ad6ba44583ba69fbdedb1412bdb3f0016`
- `resource-cleanup.json`：`3ca4ee432fdd1bb67574eef808e243bf3f62d71db5ab515a476198c7d76185a9`
- `preloads.json`：`473ac08927d5887a45e8cee88e43216e8ba39f58f9097dafd2e3b4269ef6d198`
