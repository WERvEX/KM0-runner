# KM0 Runner 路线图

本文写 Runner 下一步做什么、按什么顺序做、做到什么程度算完成。开发流程和两个仓库之间的约定见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

## 现状

- **Runner 0.2.x**：KM0 交来的任务包，Runner 在每个任务自己的 git worktree 里用 pi 执行，按 KM0 预测的档位起步，运行中的信号触发时升档，最后留下分支和执行报告。
  - 0.2.0 加了 `progress.jsonl`、优雅停止和 `--version`。
  - 0.2.1 让 Windows 上不在 PATH 里的 Git Bash 也能找到。
- **KM0 1.0**：已发布，可以一键启动 Runner、显示进度、停止运行，并自动导入结果、验收。KM0 要求 Runner 至少是 0.2.0。
- **测试**：CI 在 Linux、macOS、Windows 上跑全部测试，包括用真实 pi 跑的端到端测试，模型由脚本代答。
- **缺口**：还没有在真实模型上跑过；也还没有在真实的 Windows 机器上，从 KM0 一键跑过一次。

## 阶段

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| M0 发布 0.2 | 合并进度与停止（#10）；KM0 1.0 发布 | Runner 已合并，差 tag；KM0 已发布 v1.0.0 |
| M1 补完 Windows | 找 Git Bash；绘图测试进 CI；在真实 Windows 上跑通 | 代码部分已完成（0.2.1），差真机验证 |
| M2 真实数据 | 在真实模型上跑基线和升档，得出成本与解决率 | 等 key 和预算 |
| M3 每个任务带自己的阶梯 | 协议升级，Runner 和 KM0 都要改 | 字段已有草案（见下文），未实现 |
| M4 闭环校准 | 用真实数据校准路由规则，结果回到 KM0 | 依赖 M2 |

建议顺序：M0 → M1 → M2；M3 可以和 M2 并行，不过有了 M2 的数据更容易判断值不值得做；M4 在 M2 积累了足够的数据之后开始。

### M0 发布 0.2

- [x] [WERvEX/KM0-runner#10](https://github.com/WERvEX/KM0-runner/pull/10)：进度文件、优雅停止、`--version`
- [x] [WERvEX/KM0-runner#11](https://github.com/WERvEX/KM0-runner/pull/11)：三平台 CI，修复 Windows 上的编码和卡死问题
- [x] KM0 1.0 合并并打 `v1.0.0`
- [ ] Runner 打 `v0.2.1` tag

**完成标准**：用 KM0 1.0 的安装包，可以一键跑完 Runner。

### M1 补完 Windows

- [x] 找 Git Bash：PATH 里没有 bash，或者只有 System32 里 WSL 的那个时，用 `git` 所在的 Git 安装里的 `bash.exe`（[WERvEX/KM0-runner#12](https://github.com/WERvEX/KM0-runner/pull/12)，0.2.1）
- [x] matplotlib 加入 `dev` 依赖，绘图测试在 CI 里也跑（#12 同一个 PR）
- [ ] 在一台真实的 Windows 机器上，按普通方式安装 Git（PATH 里只有 `Git\cmd`）和 pi，从 KM0 一键跑完一个小任务包，并在中途用"停止"测一次

**完成标准**：上面最后一项通过，过程中发现的问题都已修复。

### M2 真实数据

1. 配置 `KM0_RELAY_API_KEY`，跑 `km0-runner smoke -c configs/tokeness.runner.yaml`。
2. 固定一个小任务集：用 `km0-runner dataset` 从 SWE-bench Verified 抽 dev/test 两份，或者用 KM0 生成的真实任务包。在上面跑三组：
   - 只用便宜模型：`configs/baseline-cheap.example.yaml`；
   - 只用强模型：`configs/baseline-strong.example.yaml`；
   - 按阶梯升档：`configs/tokeness.runner.yaml`。
3. 每组用 `km0-runner summarize` 汇总，再用 `km0-runner plot` 把三组画成一张"成本对解决率"的图，带 95% 置信区间。
4. 用同样的模型给 KM0 的 eval 录音带补上真实模型的录音（`pnpm eval:record`）。

**完成标准**：有一张图和一段结论，回答"按阶梯升档比全用强模型省多少钱、解决率掉多少"，写进 README。
**注意**：先跑很小的规模估一下花费，再决定任务集多大、重复几次。

### M3 每个任务带自己的阶梯

**为什么做**：KM0 1.0 会给每个任务算一条升级链（模型组合，最多三步），但 Runner 只认整次运行共用的一条 `ladder`。KM0 现在只能把模型不在这条阶梯上的任务挪到阶梯上对应档位的模型。

**字段草案**（实现前在两个仓库的 PR 里定稿）：

```json
{
  "seq": 2,
  "tier": "T2",
  "ladder": [
    {"model": "km0-3/<中档模型>", "step_limit": 50, "cost_limit": 3.0},
    {"model": "km0-3/<强模型>"}
  ]
}
```

- `ladder` 可选，从便宜到强排列。每一级的 `model` 是 pi 的 `provider/modelId`，provider 必须在这次运行用的 pi `models.json` 里；`step_limit` 和 `cost_limit` 不写就用全局配置的值。
- 有 `ladder` 的任务从第一级起步，不做"低一档探索"。`retry: true` 的任务也从第一级起步；KM0 重新交接时，要交出已经升过档的那条链。
- 没有 `ladder` 的任务行为不变：按 `tier` 和 `km0_tiers` 在全局阶梯上起步。
- 报告里新增一个可选的 `ladder` 字段，记录实际用的阶梯。`attempts[].tier` 改为记录第几级（例如 `L1`、`L2`），还是保留 T1/T2/T3 的标签？这一点要和 KM0 的后验更新一起定。

**两边要做的事**：
- Runner：`handoff.py` 读这个字段，`tiers.py` 和 `cascade.py` 支持按任务取阶梯，报告加字段，`scoring.py` 和校准不受影响，版本 0.3.0。
- KM0：`build.ts` 写出这个字段，`execution-service.ts` 把每个任务链上用到的模型都写进 `models.json`，删掉"挪到阶梯上"的提示，`fake-runner.mjs` 同步，`RUNNER_MIN_VERSION` 改为 0.3.0。

**完成标准**：Runner 0.3.0 发布；KM0 的 e2e，以及 `KM0_RUNNER_REAL` 下用真实 Runner 跑的集成测试，都在新字段下通过；旧的任务包（没有 `ladder`）照常能跑。

### M4 闭环校准

- 用 M2 和之后积累的运行目录跑 `km0-runner calibrate --dev ... --test ... -o routing-rules.json`，在 KM0 的"设置 → Agent"里导入。
- 拿 KM0 1.0 根据排行榜得出的能力先验，和实际的验收结果对照；偏差大的模型，回头调整 KM0 的模型注册表（`packages/registry`）。
- M3 落地后，校准的标签要能处理"每个任务的阶梯不同"：标签不再是"最便宜够用的 T 档"，而是"最便宜够用的那一级"。这需要在实现 M3 时一并设计。

**完成标准**：在 test 集上，用校准后的规则算出的期望成本低于校准前，结果写进 README。

## 暂不做

- 把 Runner 产出的分支合并进用户的仓库。Runner 只留分支，KM0 1.0 的路线图也明确不做这件事。
- 让 Runner 驱动 pi 以外的 Agent（Claude Code、Codex 等）。KM0 的"自定义执行命令"已经覆盖了这类需求。
- 发布到 PyPI，或者做 GitHub Release 的自动化。等有外部用户再说。
