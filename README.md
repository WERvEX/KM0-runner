# KM0 Runner

KM0 Runner executes the task packages [KM0](https://github.com/WERvEX/KM0) hands off. Each task runs in its
own git worktree with [pi](https://github.com/earendil-works/pi) as the coding agent, starts on the model tier
KM0 predicted, moves up the ladder when the run gets stuck, and ends as a branch plus an execution report.
KM0 then accepts or rejects each task and uses the verdicts to calibrate its routing.

```
KM0 predicts a tier ─► Runner starts there ─► signals escalate ─► branch + report ─► KM0 accepts ─► calibration
```

- **Task-level routing** is KM0's: its rules predict the tier a task needs.
- **Trajectory-level escalation** is the Runner's: while a tier works, rule-based signals watch pi's tool
  calls and move the task one tier up when it gets stuck.
- **Acceptance** is KM0's: a strong model checks the change against the task's EARS criteria. A rejected task
  comes back one tier up. The Runner's own test run is only an escalation signal and evidence for KM0.
- **Calibration** learns from the cheapest tier KM0 accepted, and a small share of low-risk tasks is
  started one tier lower on purpose so cheaper tiers get a chance to prove themselves.

## Install

```bash
npm install -g @earendil-works/pi-coding-agent@1.3.0   # the pinned pi (Node 22.19 or newer)
pip install -e ".[dev]"                                 # add ",swebench" for the optional benchmark
pytest
```

The Runner works on Linux, macOS and Windows, and CI runs the tests on all three. It needs `git` and `bash` on
PATH, because each task's test command runs in `bash -lc`. On Windows that bash is Git for Windows' (Git
Bash), not WSL's.

Models come from pi. A relay or any OpenAI-compatible endpoint is a provider in pi's `models.json`
(`configs/models.example.json`); the Runner config names models as `provider/modelId`.

### Tokeness relay

`configs/tokeness.runner.yaml` and `configs/tokeness.models.json` are ready for the
[Tokeness](https://docs.tokeness.ai) relay. pi talks to it in Anthropic format (`https://n.tokeness.dev`);
the `tokeness-openai` provider in the same file is the OpenAI-compatible route (`https://n.tokeness.dev/v1`)
if the first one is refused. The key is never in a file: pi reads it from `KM0_RELAY_API_KEY`.

```bash
export KM0_RELAY_API_KEY=...                          # or set it as an environment secret
km0-runner smoke -c configs/tokeness.runner.yaml
```

## Run

```bash
cp configs/runner.example.yaml runner.yaml            # fill in the ladder's models
km0-runner smoke -c runner.yaml                       # stage 0: pi edits a file, switches model once
km0-runner run path/to/km0 --repo path/to/repo -c runner.yaml
km0-runner summarize runs/<run_id>
```

`run` takes a `km0/` folder, a repository holding one, or KM0's package JSON. Tasks run one at a time in
dependency order. The user's checkout is never touched: each task gets a worktree on branch
`km0/<run_id>/NN`, starting from the run's starting commit or from its upstream tasks' branches merged in
order. The Runner commits the task's changes there (never `km0/`), removes the worktree and keeps the branch.
It never pushes or merges.

When KM0 rejects a task, it hands the task off again one tier higher (`retry: true` in `tasks.json`; such a
task is never explored lower). Redo only that task on top of the tasks KM0 accepted:

```bash
km0-runner run path/to/km0 --repo path/to/repo -c runner.yaml --tasks 2 --reuse <earlier run_id>
```

Upstream tasks left out of `--tasks` then start from the earlier run's branches.

### Following and stopping a run

While it works the Runner appends events to `runs/<run_id>/progress.jsonl`, one JSON object per line, flushed
as they happen, so KM0 (or `tail -f`) can follow the run and pick it up again after a restart: `run_started`,
`task_started`, `attempt_started` and `attempt_finished` (tier, model, cost), `escalation`, `test_result`,
`merge_resolution`, `task_finished` (status `passed`, `unjudged`, `failed`, `blocked` or `cancelled`, and cost)
and `run_finished`. Each line carries `seq` and an `at` timestamp; the fields are listed in
`src/km0_runner/progress.py`.

To stop a run, send it SIGTERM (or press Ctrl+C), or create `runs/<run_id>/STOP`, which works on Windows too.
The Runner aborts pi, commits what the current task has so far to its branch, reports that task and every task
not yet run as `cancelled`, removes the worktrees and writes the summary. `run` exits with 0 when every task
finished without a failure, 1 when some failed or were blocked, and 3 when the run was stopped.
`km0-runner --version` prints the version, which KM0 checks before it starts a run.

## How a task runs

1. **Start tier.** KM0's tier maps onto the ladder through `km0_tiers`. With `exploration.rate` set, that
   share of tasks without risk flags starts one tier lower and is marked `explored`.
2. **Execute.** pi starts in RPC mode in the worktree with only `read, bash, edit, write`, and without the
   repository's own pi settings, extensions, skills, prompt templates or MCP servers. The prompt is KM0's task
   prompt plus a short Runner note: run the test command, do not commit, push or switch branches.
3. **Watch.** Every tool event goes through the signal monitor:
   | signal | fires when |
   | --- | --- |
   | `test_fail_streak` | test runs fail this many times in a row |
   | `same_file_edits` | one file is edited or written this many times |
   | `repeat_command` | the same tool call is made this many times in a row |
   | `tool_error_streak` | tool calls return errors this many times in a row |
   | `step_budget` / `cost_budget` | the tier's turn or USD limit is spent |
   A signal aborts the run.
4. **Check.** When pi settles, the Runner runs the task's `testCommand` in the worktree. A pass ends the task.
   A failure, a signal or a spent budget moves it one tier up. A task without a test command ends after one
   run and is left for KM0 to judge.
5. **Handoff** to the higher tier:
   - `discard` (default): a new session on the stronger model, the repository as it is, and a note saying
     where the last attempt stopped and to start with `git diff`;
   - `continue`: switch the model inside the same session and send the note;
   - `restart`: reset the worktree and start over in a new session.
6. **Finish.** Commit, write the report and the diff, remove the worktree. A failure on the top tier marks
   the task failed, and tasks that depend on it are `blocked`.

When two upstream branches conflict (parallel tasks that edited the same lines), pi first resolves the
conflict on the task's start tier, keeping both sides, and the task's test command must pass on the result.
The Runner commits the merge and the task starts on top of it, so the task's own diff leaves the resolution
out; `merge_diff` shows how the conflict was resolved. The resolution counts toward the task's cost but
not toward calibration. If it fails, the task is `blocked`.

## Output

```
runs/<run_id>/
  run.json                      config, hash, pi version, starting commit, task order
  reports/NN.json               execution report per task
  diffs/NN.diff                 the task's change, for KM0's acceptance
  diffs/NN.merge.diff           how a conflict between upstream branches was resolved, when there was one
  events/NN-attempt-K-T1.jsonl  pi's event stream per attempt
  logs/NN-pi.log                pi's stderr
  summary.json                  cost, tier use, pass rate, and the all-top-tier estimate
  acceptance.jsonl              KM0's verdicts, written by KM0
```

The execution report (`report_version: 2`, `src/km0_runner/report.py`):

| field | meaning |
| --- | --- |
| `run_id`, `project_id`, `task_id` | the run, KM0's project, and the task's seq (`NN`) |
| `predicted_tier`, `predicted_features`, `predicted_score` | KM0's routing, passed through for calibration |
| `start_tier`, `explored` | where the task started, and whether that was an exploration one tier lower |
| `solved_tier` | the tier on which the Runner's test command passed; KM0's acceptance decides the rest |
| `escalations` | each escalation: from, to, turn, signal and detail |
| `cost`, `steps`, `tokens`, `wall_time` | totals over all attempts, from pi's session stats |
| `test_command`, `test_result` | the Runner's own check |
| `failure_category` | `tests_failed`, `budget_exhausted`, `signal_on_top_tier`, `blocked` or `error` |
| `branch`, `base_commit`, `commit`, `diff` | where the change is |
| `acceptance`, `depends_on` | KM0's criteria and dependencies, passed through |
| `merge_resolved`, `merge_diff` | files that conflicted between upstream branches, and how they were resolved |
| `config_hash`, `pi_version`, `runner_version` | for reproducing a run |
| `attempts` | per attempt: purpose (`task`, or `merge` for a conflict resolution), tier, model, turns, tokens, cost, stop reason, signal, test result, events file |

`summary.json` adds the run's total cost, attempts and cost per tier, the pass rate, and an estimate of
running everything on the top tier: the same tokens priced at the top rung's rates from `models.json`.

KM0 writes `acceptance.jsonl` into the run directory, one verdict per line:
`task_id`, `accepted`, `reason`, `judged_by`, `user_override` (a spot check's verdict, which wins) and
`judged_at`.

## Calibrating KM0's routing rules

```bash
km0-runner calibrate --dev runs/r1 runs/r2 --test runs/r3 -o routing-rules.json
```

Each KM0 task becomes one sample, gathered over every run of it: an attempt that escalated, failed its tests
or was rejected by KM0 shows the task needs more than that tier; KM0's acceptance (or, before KM0 has judged,
a passing test run) shows the tier is enough. The label is the cheapest tier that was enough. A label with no
attempt below it is only an upper bound and is counted as such; exploration is how those get resolved.

The fit replays KM0's rules (`src/km0_runner/scoring.py`, a port of `packages/core/src/routing/scoring.ts`
that must stay in step with it). It changes the tier limits first, then each feature value's points, and
minimises the expected cost of the cascade from measured per-tier costs; hard rules stay fixed. KM0 imports
`routing-rules.json` in Settings → Agents.

```bash
km0-runner plot runs/strong runs/cheap runs/cascade -o scatter.png   # cost vs solve rate, 95% bootstrap
```

## Testing without an API key

`km0-runner bridge <folder>` is a stand-in OpenAI-compatible provider. Point a provider at it in pi's
`models.json` (`llm_bridge.models_json` writes one), and each request pi makes is written to
`<folder>/NNNN.request.md` (the conversation as text: system prompt, tools, messages, tool calls and
results) and `NNNN.request.json`. The bridge then waits for `NNNN.response.json`, either
`{"text": "..."}` or `{"tool_calls": [{"name": "bash", "arguments": {"command": "pytest -q"}}]}`. Someone
answers each request, such as a fresh model session that reads only that request. KM0 itself can use
the same bridge: an OpenAI-compatible model profile with its base URL and structured output mode "prompt"
sends triage, questions, documents, task breakdown and acceptance through it too, so the whole loop runs
without a key. Answers are cached by
request content, so a rerun only asks again where the conversation changed. This tests the Runner and the
prompts rather than a provider; token counts are rough estimates. `--script answers.jsonl` replays fixed
answers instead, which is how `tests/test_real_pi.py` drives the real pi end to end.

## SWE-bench (optional)

```bash
km0-runner dataset --source verified --size 60 -o data
km0-runner swebench -c runner.yaml --instances data/dev.jsonl --predicted km0.json --repeats 3
```

Each instance's repository is cloned once, checked out at its base commit in a worktree, run through the
same cascade, and graded by the official harness (needs the `swebench` extra and Docker). This is a
regression check for Runner changes, not a goal.

## Status

Version 0.2.0, the version KM0 1.0 requires. KM0 1.0 starts the Runner itself: it generates the Runner config
and pi's `models.json` from its own providers, follows `progress.jsonl`, can stop a run, and imports and judges
the reports when the run ends.

Stages 0 and 1 of the plan are in place and tested on Linux, macOS and Windows. A scripted fake pi covers
signals, the three handoffs, budgets, dependencies, blocking, exploration, progress events and stopping, and
the real pi runs end to end against the scripted bridge.

Not done yet:

- **Runs on real models.** The baselines and the cascade have not been run on real models yet, so the cost
  against solve rate comparison and calibration on real data are still to come. Those runs need the relay
  configured in pi's `models.json`.
- **A ladder per task.** KM0 1.0 works out an escalation chain for each task, but the Runner takes one
  ladder for the whole run. KM0 moves a task whose chosen model is not on that ladder onto the ladder's model.
