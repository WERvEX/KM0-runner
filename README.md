# KM0 Runner

KM0 Runner takes the task packages [KM0](https://github.com/WERvEX/KM0) hands off, runs each task on a ladder
of models (T1 cheap → T3 strong) in a sandbox, judges the result by tests, and writes an execution report KM0
reads back to calibrate its routing:

```
KM0 predicts a tier ─► Runner starts there ─► trajectory signals escalate ─► tests judge ─► report ─► KM0 calibrates
```

- **Task-level routing** is KM0's: its rules predict the tier a task needs.
- **Trajectory-level escalation** is the Runner's: while a tier works, rule-based signals watch for it getting
  stuck and move the task one tier up.
- **Feedback**: each report records the predicted tier against the tier that actually solved the task.

The agent itself is [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent); SWE-bench tasks run in the
official SWE-bench images and are judged by the official harness. What the Runner adds is the tier ladder, the
escalation signals, the handoff between tiers and the report.

## Install

```bash
pip install -e ".[dev]"            # add ",swebench" for the SWE-bench harness judge
pytest
```

## Run

Copy a config from `configs/` and fill in the model names (any LiteLLM name mini-swe-agent accepts), then:

```bash
# A KM0 package unpacked into a repository (km0/tasks.json + km0/tasks/NN.md), judged by a test command
km0-runner km0 -c cascade.yaml -o runs/demo --repo ../my-repo --test-command "pnpm test"

# SWE-bench instances (JSON/JSONL rows with instance_id and problem_statement), judged by the harness
km0-runner swebench -c cascade.yaml -o runs/dev --instances dev.jsonl --predicted km0-tiers.json

# Stage 0 baselines: a one-tier ladder runs everything on that tier
km0-runner swebench -c baseline-strong.yaml -o runs/strong --instances dev.jsonl

# Cost, solve rate and KM0's predicted-vs-solved matrix
km0-runner summarize runs/dev
```

Model API keys are read from the environment as LiteLLM expects (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …).

## How a task runs

1. Start on KM0's predicted tier (`--start-tier` overrides it; a ladder without that tier starts on the next
   configured one up).
2. After every step the signal monitor checks:
   | signal | fires when |
   | --- | --- |
   | `test_fail_streak` | test runs fail this many times in a row |
   | `same_file_edits` | one file is edited this many times |
   | `repeat_command` | the same command runs this many times in a row |
   | `step_budget` | the tier's step limit is spent |
   | `cost_budget` | the tier's cost limit is spent |
   | `failed_submit` | the agent submits but the judge says the tests fail |
3. On a signal the task moves to the next tier. The handoff is one of:
   - `discard` (default): drop the lower tier's trajectory, keep the repository, pass a short note
     (following *The Handoff Tax*, 2026);
   - `full`: keep the repository and pass the whole transcript;
   - `restart`: reset the repository and start over.
4. A judged pass, or a failure on the top tier, ends the task.

Thresholds are starting guesses to tune on the dev set (`configs/cascade.example.yaml`).

## Output

```
runs/<name>/
  reports.jsonl                  one execution report per line
  <task>/report.json             the same report
  <task>/steps.jsonl             one line per command: attempt, tier, step, return code, signal
  <task>/attempt-N-T1.traj.json  mini-swe-agent trajectory of each attempt
```

The execution report (`src/km0_runner/report.py`):

| field | meaning |
| --- | --- |
| `task_id` | KM0 task seq, or SWE-bench instance id |
| `predicted_tier` | KM0's tier |
| `solved_tier` | tier the task was solved on; null if unsolved or unjudged |
| `escalations` | each escalation: from, to, step and signal |
| `cost` / `steps` / `wall_time` | totals over all attempts |
| `test_result` | the judge's verdict on the last attempt |
| `failure_category` | `tests_failed`, `budget_exhausted`, `signal_on_top_tier`, `no_submission` or `error` |
| `config_hash` | hash of the config, Runner and mini-swe-agent versions, for reproducing a run |
| `attempts` | per-attempt tier, model, exit status, signal, cost and trajectory path |

## Status

Stage 1 code (ladder, signals, handoffs, reports) is in place and tested with scripted models. Stage 0, the
two baselines on a small SWE-bench Verified subset, is next and needs the three models chosen and API keys.
