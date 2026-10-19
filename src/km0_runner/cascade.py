"""The cascade: start on the predicted tier, escalate on signals or failed tests, hand off, report."""

from __future__ import annotations

import copy
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

from minisweagent import Environment, Model
from minisweagent.config import get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.models import get_model
from minisweagent.utils.serialize import recursive_merge

from . import __version__
from .agent import ESCALATE, WatchedAgent
from .handoff import RunTask
from .judge import Judge, NoJudge
from .report import Attempt, Escalation, ExecutionReport, FailureCategory, TestResult
from .signals import SignalMonitor
from .tiers import RunnerConfig, Tier, TierModel

logger = logging.getLogger("km0_runner")

EnvFactory = Callable[[RunTask, dict], Environment]
"""Builds the task's environment from the merged `environment` config section."""
ModelFactory = Callable[[TierModel, dict], Model]
"""Builds a tier's model from its rung and the agent config's `model` section."""

PATCH_COMMAND = "git add -A -- . ':!**/__pycache__/**' ':!*.pyc' && git diff --cached"

HANDOFF_NOTE = """\
<handoff>
A cheaper model already worked on this task and was stopped ({reason}).
The repository is in the state it left: its changes may be partial or wrong.
Run `git status` and `git diff` to see them, then keep, fix or revert them as needed.
</handoff>"""

TRAJECTORY_NOTE = """\
<handoff>
A cheaper model already worked on this task and was stopped ({reason}).
The repository is in the state it left. Its full transcript follows.
<transcript>
{transcript}
</transcript>
</handoff>"""


def default_env_factory(task: RunTask, environment: dict) -> Environment:
    if task.source == "swebench":
        from minisweagent.run.benchmarks.swebench import get_sb_environment

        return get_sb_environment({"environment": environment}, task.instance or {})
    return get_environment(environment, default_type="local")


def default_model_factory(rung: TierModel, model_section: dict) -> Model:
    config = recursive_merge(
        model_section,
        {"model_name": rung.model_name, "model_kwargs": rung.model_kwargs},
        {"model_class": rung.model_class} if rung.model_class else {},
    )
    return get_model(config=config)


def transcript(messages: list[dict], limit: int) -> str:
    """The lower tier's conversation after the system and task messages, newest kept when cut."""
    lines = []
    for message in messages[2:]:
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
        lines.append(f"[{message.get('role')}]\n{content or ''}")
    text = "\n\n".join(lines)
    return text if len(text) <= limit else "…(earlier steps cut)\n" + text[-limit:]


class Cascade:
    def __init__(
        self,
        config: RunnerConfig,
        out_dir: str | Path,
        judge: Judge | None = None,
        env_factory: EnvFactory = default_env_factory,
        model_factory: ModelFactory = default_model_factory,
        patch_command: str = PATCH_COMMAND,
    ):
        self.config = config
        self.out_dir = Path(out_dir)
        self.judge = judge or NoJudge()
        self.env_factory = env_factory
        self.model_factory = model_factory
        self.patch_command = patch_command
        self.base = get_config_from_spec(config.agent_config)

    def run(self, task: RunTask, start_tier: Tier | None = None) -> ExecutionReport:
        config = self.config
        tier = config.start_tier(start_tier or task.predicted_tier or "T1")
        task_dir = self.out_dir / task.task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        steps_log = (task_dir / "steps.jsonl").open("w", encoding="utf-8")

        attempts: list[Attempt] = []
        escalations: list[Escalation] = []
        solved: Tier | None = None
        failure: FailureCategory | None = None
        note = ""
        started = time.time()
        env = self.env_factory(task, recursive_merge(self.base.get("environment", {}), config.environment))
        patch = ""
        try:
            while True:
                attempt, agent = self._attempt(task, tier, env, note, len(attempts) + 1, task_dir, steps_log)
                patch = self._patch(env)
                if attempt.exit_status == "Submitted":
                    attempt.test_result = self.judge.judge(task, env, patch)
                attempts.append(attempt)

                passed = attempt.test_result.passed if attempt.test_result else None
                if attempt.exit_status == "Submitted" and passed is not False:
                    # An unjudged submission ends the cascade too; solved_tier stays empty without a verdict.
                    solved = tier if passed else None
                    break
                reason = self._reason(attempt)
                upper = config.above(tier)
                if attempt.exit_status == "Submitted" and not config.signals.escalate_on_failed_submit:
                    upper = None
                if upper is None:
                    failure = self._failure(attempt)
                    break
                escalations.append(
                    Escalation(
                        from_tier=tier,
                        to_tier=upper,
                        step=attempt.steps,
                        signal=attempt.signal or reason,
                        detail=self._detail(attempt, agent),
                    )
                )
                note = self._handoff(env, agent, reason)
                tier = upper
        except Exception as error:  # an environment or API failure ends the task, not the batch
            logger.exception("task %s crashed", task.task_id)
            failure = "error"
            crash = f"{type(error).__name__}: {error}"
            attempts.append(
                Attempt(
                    tier=tier, model=config.tier(tier).model_name, exit_status="Error", signal=None,
                    steps=0, cost=0.0, wall_time=0.0,
                    test_result=TestResult(passed=None, judge="none", detail=crash),
                    trajectory=None,
                )
            )  # fmt: skip
        finally:
            steps_log.close()
            if hasattr(env, "cleanup"):
                env.cleanup()

        final = attempts[-1].test_result if attempts else None
        report = ExecutionReport(
            task_id=task.task_id,
            source=task.source,
            title=task.title,
            predicted_tier=task.predicted_tier,
            start_tier=attempts[0].tier if attempts else tier,
            solved_tier=solved,
            escalations=escalations,
            cost=sum(a.cost for a in attempts),
            steps=sum(a.steps for a in attempts),
            wall_time=round(time.time() - started, 2),
            test_result=final,
            failure_category=failure,
            handoff=config.handoff,
            config_hash=config.hash(),
            runner_version=__version__,
            attempts=attempts,
            patch=patch,
        )
        (task_dir / "report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
        return report

    def _attempt(self, task, tier, env, note, number, task_dir, steps_log):
        rung = self.config.tier(tier)
        monitor = SignalMonitor(self.config.signals, step_limit=rung.step_limit)
        model = self.model_factory(rung, copy.deepcopy(self.base.get("model", {})))
        trajectory = task_dir / f"attempt-{number}-{tier}.traj.json"
        agent_section = recursive_merge(
            self.base.get("agent", {}), {"step_limit": 0, "cost_limit": rung.cost_limit, "output_path": trajectory}
        )
        agent = WatchedAgent(model, env, monitor=monitor, **agent_section)
        original = monitor.observe

        def logged(command: str, returncode: int | None, output: str = ""):
            signal = original(command, returncode, output)
            record = {"attempt": number, "tier": tier, "step": monitor.steps, "command": command}
            record |= {"returncode": returncode, "signal": signal.name if signal else None}
            steps_log.write(json.dumps(record, ensure_ascii=False) + "\n")
            steps_log.flush()
            return signal

        monitor.observe = logged  # type: ignore[method-assign]
        prompt = f"{task.prompt}\n\n{note}" if note else task.prompt
        started = time.time()
        result = agent.run(prompt)
        exit_status = result.get("exit_status", "")
        signal = agent.signal.name if agent.signal else None
        if exit_status == "LimitsExceeded":
            signal = "cost_budget"
        return (
            Attempt(
                tier=tier,
                model=rung.model_name,
                exit_status=exit_status,
                signal=signal,
                steps=agent.n_calls,
                cost=round(agent.cost, 6),
                wall_time=round(time.time() - started, 2),
                test_result=None,
                trajectory=str(trajectory.relative_to(self.out_dir)),
            ),
            agent,
        )

    def _patch(self, env: Environment) -> str:
        try:
            return env.execute({"command": self.patch_command}).get("output", "")
        except Exception:  # an agent may have left the repository unusable
            return ""

    def _reason(self, attempt: Attempt) -> str:
        if attempt.signal:
            return attempt.signal
        if attempt.exit_status == "Submitted":
            return "failed_submit"
        return attempt.exit_status or "no_submission"

    def _detail(self, attempt: Attempt, agent: WatchedAgent) -> str:
        if agent.signal:
            return agent.signal.detail
        if attempt.test_result and attempt.test_result.detail:
            return attempt.test_result.detail[:500]
        return attempt.exit_status

    def _failure(self, attempt: Attempt) -> FailureCategory:
        if attempt.exit_status == "Submitted":
            return "tests_failed"
        if attempt.signal in ("step_budget", "cost_budget"):
            return "budget_exhausted"
        if attempt.signal:
            return "signal_on_top_tier"
        return "no_submission"

    def _handoff(self, env: Environment, agent: WatchedAgent, reason: str) -> str:
        mode = self.config.handoff
        if mode == "restart":
            env.execute({"command": self.config.reset_command})
            return ""
        if mode == "full":
            text = transcript(agent.messages, self.config.trajectory_note_chars)
            return TRAJECTORY_NOTE.format(reason=reason, transcript=text)
        return HANDOFF_NOTE.format(reason=reason)
