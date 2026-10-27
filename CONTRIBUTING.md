# 开发流程

KM0 Runner 和 [KM0](https://github.com/WERvEX/KM0) 是两个仓库，靠文件和命令行协议对接。这份文档写两件事：一个改动怎么从分支走到发布，以及改到两个仓库共享的东西时要一起改什么。要做什么、按什么顺序做，见 [docs/ROADMAP.md](docs/ROADMAP.md)。

## 一个改动怎么走

1. **一个改动一个分支、一个 PR。** 从最新的 `main` 开分支。PR 描述按这个仓库一直用的格式写：
   - Before：改之前是什么情况；
   - After：改完以后怎样；
   - How：怎么做的；
   - 测试：跑了什么、结果如何，还有哪些没覆盖到。
2. **合并前要跑的检查：**
   - `pytest` 全部通过。
   - `ruff check src tests` 不新增问题；原有的问题不在这个 PR 的范围里。
   - 改到和 pi 交互的部分（`pi_client.py`、`cascade.py`、bridge）时，确认 `tests/test_real_pi.py` 没有被跳过：本地装上锁定版本的 pi，或者设置 `KM0_TEST_PI`。
3. **CI 三个平台必须全绿。** CI 在 Linux、macOS、Windows 上跑。它只在 PR 和 `main` 上自动触发；在其他分支上调试时，可以在 Actions 页面手动运行 CI（`workflow_dispatch`）。
4. **一律 squash 合并。** `main` 上一个 PR 对应一个提交，标题末尾带 `(#N)`。调试用的临时提交在 squash 时消失，但合并时的提交信息要重新写准确。

### 跨平台

- 读写文本一律写明 `encoding="utf-8"`。Windows 的默认编码是系统代码页（中文系统是 GBK），不是 UTF-8。
- 启动外部程序时，程序名先经过 `pi_client.resolve()`，Windows 上才能找到 `pi.cmd` 和 Git Bash。
- 子进程不需要输入时给 `stdin=subprocess.DEVNULL`。在 Windows 上，子进程继承一个正被别的线程读着的管道会卡死。
- 停止运行在 Windows 上只能靠 `STOP` 文件，没有 SIGTERM。

## 和 KM0 共享的东西

下表每一行都是两个仓库各有一份的东西，改了一边就要检查另一边。

| 共享的东西 | Runner 这边 | KM0 那边 |
| --- | --- | --- |
| 交接包 `km0/tasks.json`、`manifest.json` | `handoff.py` | `packages/core/src/handoff/build.ts` |
| 执行报告（`report_version: 2`）、`summary.json`、`run.json` | `report.py`、`summary.py`、`runner.py` | `packages/shared/src/runner.ts`、`apps/desktop/src/main/services/runner-service.ts` |
| KM0 写回的 `acceptance.jsonl` | `acceptance.py`、`calibrate.py` | `runner-service.ts` |
| `progress.jsonl`、`STOP` 文件、退出码、`--version` | `progress.py`、`cli.py` | `apps/desktop/src/main/services/execution-service.ts`、`e2e/fixtures/fake-runner.mjs` |
| Runner 配置和 pi 的 `models.json` | `tiers.py` | `execution-service.ts` 里生成配置的部分 |
| 打分规则 | `scoring.py`（移植版） | `packages/core/src/routing/scoring.ts` |
| 校准结果 `routing-rules.json` | `calibrate.py` | 设置 → Agent 里的导入 |
| 锁定的 pi 版本（1.3.0） | README、CI | `packages/shared/src/execution.ts` 的 `PI_VERSION` |
| 最低 Runner 版本 | `pyproject.toml`、`src/km0_runner/__init__.py` | `execution.ts` 的 `RUNNER_MIN_VERSION` |

规则：

- **兼容**：新增字段一律可选。Runner 读到不认识的字段要忽略，缺了新字段要退回原来的行为，这样两个仓库不必同一天发布。只有删除字段或改变字段含义时，才升 `report_version`。
- **`fake-runner.mjs`**：KM0 的 e2e 用它冒充 Runner，因为 KM0 的 CI 不装 Python。Runner 的协议一变，它就要同步改，否则 KM0 的测试会和真实的 Runner 对不上。KM0 里设置 `KM0_RUNNER_REAL=<本仓库目录>` 时，会用真实的 Runner 跑一遍集成测试（`execution-service.test.ts`）。改协议的 PR 要在本地跑过这个测试。
- **打分规则**：KM0 改了 `scoring.ts`，同一轮就要改 `scoring.py`，否则校准时重放的是旧规则。
- **pi 版本**：要升级就两边一起升：Runner 的 README 和 CI、KM0 的 `PI_VERSION`。

### 版本号

| 改动 | 版本 | KM0 要做什么 |
| --- | --- | --- |
| 修 bug，协议不变 | patch（0.2.x） | 不用动 |
| 协议新增了 KM0 要用的能力 | minor（0.3.0） | 抬高 `RUNNER_MIN_VERSION` |
| 删除字段或改变含义 | minor，并升 `report_version` | KM0 同时兼容新旧两种格式 |

协议有改动时，两个仓库各开一个 PR，在描述里互相链接。合并顺序是：**先合并 Runner 并发布，再合并 KM0**；KM0 的 PR 里同时抬高 `RUNNER_MIN_VERSION`。

## 发布

1. 在 PR 里改好版本号（`pyproject.toml` 和 `src/km0_runner/__init__.py` 两处），并更新 README 的 Status 一节。
2. 合并后，在 `main` 上打 tag：

   ```bash
   git fetch origin main
   git tag -a vX.Y.Z -m "KM0 Runner X.Y.Z" origin/main
   git push origin vX.Y.Z
   ```

3. 目前没有发布用的 workflow。用户用 `pip install` 装这个仓库，KM0 检查 PATH 里 `km0-runner --version` 的输出。等有外部用户时，再考虑 GitHub Release 或 PyPI。
