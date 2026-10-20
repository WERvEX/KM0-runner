"""The cascade for one task: start on a tier, watch pi's tool events, escalate, hand off, check, report."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .handoff import RunTask
from .judge import run_test_command
from .pi_client import PiClient, PiError, RunOutcome, tool_text
from .report import Attempt, Escalation, ExecutionReport, FailureCategory, StopReason, TestResult, Tokens
from .signals import Signal, SignalMonitor
from .tiers import RunnerConfig, Rung, Tier
from .workspace import Workspace, Workspaces

RUNNER_NOTE = """

---
Notes from the runner executing this task:
- Work only inside the current directory, which is a dedicated checkout for this task.
- Do not commit, push, or switch branches; the runner commits your changes when you finish.
{test_line}- When the work is done, stop and summarise what you changed."""
TEST_LINE = "- Before you finish, run `{command}` and make it pass.\n"

HANDOFF_NOTE = """A previous attempt at this task, on another model, was stopped: {detail}.
Its changes are still in the working tree. Start with `git diff` to see them, keep what is right, fix what is not, and finish the task.
Changed files so far:
{diffstat}"""

MERGE_NOTE = """This checkout is in the middle of `git merge {branch}`: two earlier tasks, both finished, changed the same lines.
Conflicted files:
{files}
Resolve every conflict so that both sides' changes are kept, remove all conflict markers, and change nothing else.
Do not run `git merge --abort`, commit, or switch branches; the runner commits the merge.
{test_line}When done, stop and summarise how you resolved each conflict."""
MERGE_TEST_LINE = "Afterwards run `{command}` and make it pass without undoing what either side did.\n"

PiFactory = Callable[[Rung, Path, Path], PiClient]
"""Starts pi on a rung in a workspace, logging stderr to the given file."""
Check = Callable[[RunTask, Workspace], TestResult]


def runner_prompt() -> str:
    """The Runner's fixed prompt text, hashed into `config_hash`."""
    return RUNNER_NOTE + TEST_LINE + HANDOFF_NOTE + MERGE_NOTE + MERGE_TEST_LINE


def task_message(task: RunTask, test_command: str | None) -> str:
    test_line = TEST_LINE.format(command=test_command) if test_command else ""
    return task.prompt + RUNNER_NOTE.format(test_line=test_line)


@dataclass
class _Watch:
    """Per-attempt state the event handler updates."""

    rung: Rung
    monitor: SignalMonitor
    events: Any
    cost: float = 0.0
    signal: Signal | None = None
    pending: dict[str, dict[str, Any]] | None = None

    def handle(self, event: dict[str, Any]) -> StopReason | None:
        kind = event.get("type")
        if kind != "message_update":
            self.events.write(json.dumps(event, ensure_ascii=False) + "\n")
        if kind == "tool_execution_start":
            self.pending = self.pending or {}
            self.pending[event.get("toolCallId", "")] = event.get("args") or {}
            self.signal = self.monitor.tool_start(event.get("toolName", ""), event.get("args") or {})
        elif kind == "tool_execution_end":
            args = (self.pending or {}).pop(event.get("toolCallId", ""), {})
            output = tool_text(event.get("result"))
            self.signal = self.monitor.tool_end(event.get("toolName", ""), args, bool(event.get("isError")), output)
        elif kind == "message_end" and (event.get("message") or {}).get("role") == "assistant":
            usage = (event["message"].get("usage") or {}).get("cost") or {}
            self.cost += float(usage.get("total") or 0)
        elif kind == "turn_end":
            self.monitor.turn()
            if self.cost >= self.rung.cost_limit:
                self.signal = Signal("cost_budget", f"spent ${self.cost:.4f} of ${self.rung.cost_limit}", self.monitor.steps)
            elif self.monitor.steps >= self.rung.step_limit:
                self.signal = Signal("step_budget", f"used {self.monitor.steps} of {self.rung.step_limit} turns", self.monitor.steps)
        if self.signal is None:
            return None
        if self.signal.name in ("step_budget", "cost_budget"):
            return self.signal.name  # type: ignore[return-value]
        return "signal"


def _tokens(stats: dict[str, Any]) -> Tokens:
    raw = stats.get("tokens") or {}
    return Tokens(
        input=raw.get("input", 0), output=raw.get("output", 0),
        cache_read=raw.get("cacheRead", 0), cache_write=raw.get("cacheWrite", 0),
    )  # fmt: skip


def _minus(after: Tokens, before: Tokens) -> Tokens:
    return Tokens(**{name: getattr(after, name) - getattr(before, name) for name in Tokens.model_fields})


class Cascade:
    def __init__(
        self,
        config: RunnerConfig,
        run_dir: str | Path,
        run_id: str,
        workspaces: Workspaces,
        pi_factory: PiFactory,
        pi_version: str | None,
        check: Check | None = None,
        project_id: int | str | None = None,
    ):
        self.config = config
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.workspaces = workspaces
        self.pi_factory = pi_factory
        self.pi_version = pi_version
        self.check = check or self._test_command
        self.project_id = project_id
        self.config_hash = config.hash(pi_version, runner_prompt())

    def test_command(self, task: RunTask) -> str | None:
        return task.test_command or self.config.test_command

    def _test_command(self, task: RunTask, workspace: Workspace) -> TestResult:
        return run_test_command(
            self.test_command(task), workspace.path, self.config.test_timeout,
            self.config.docker_image if self.config.isolation == "docker" else None,
        )  # fmt: skip

    def blocked(self, task: RunTask, reason: str, attempts: list[Attempt] | None = None) -> ExecutionReport:
        attempts = attempts or []
        return self._report(
            task, start=None, explored=False, failure="blocked", detail=reason, attempts=attempts,
            cost=round(sum(a.cost for a in attempts), 6), steps=sum(a.steps for a in attempts),
        )  # fmt: skip

    def _attempt(
        self, pi: PiClient, rung: Rung, message: str, path: Path, purpose: str = "task"
    ) -> tuple[Attempt, RunOutcome, Signal | None]:
        """Play one prompt on pi, watching budgets and signals; the attempt, pi's outcome and the signal that fired."""
        before = pi.stats()
        with path.open("w", encoding="utf-8") as events:
            watch = _Watch(rung, SignalMonitor(self.config.signals), events)
            attempt_start = time.monotonic()
            outcome = pi.run(message, watch.handle, timeout=self.config.pi.turn_timeout)
        after = pi.stats()
        stop: StopReason = outcome.stop or ("error" if outcome.error else "settled")
        attempt = Attempt(
            purpose=purpose,  # type: ignore[arg-type]
            tier=rung.name,
            model=rung.model,
            steps=watch.monitor.steps,
            tokens=_minus(_tokens(after), _tokens(before)),
            cost=round(float(after.get("cost") or 0) - float(before.get("cost") or 0), 6),
            wall_time=round(time.monotonic() - attempt_start, 2),
            stop_reason=stop,
            signal=watch.signal.name if watch.signal else None,
            events=str(path.relative_to(self.run_dir)),
        )
        return attempt, outcome, watch.signal

    def resolve_merge(
        self, task: RunTask, tier: Tier, path: Path, files: list[str], branch: str, attempts: list[Attempt]
    ) -> str | None:
        """Have pi resolve a conflict between the task's upstream branches, on the task's start tier.
        Appends its attempt to `attempts`; returns None when resolved, otherwise why not."""
        rung = self.config.rung(tier)
        events_dir = self.run_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        command = self.test_command(task)
        message = MERGE_NOTE.format(
            branch=branch, files="\n".join(f"- {name}" for name in files),
            test_line=MERGE_TEST_LINE.format(command=command) if command else "",
        )  # fmt: skip
        pi = self.pi_factory(rung, path, self.run_dir / "logs" / f"{task.task_id}-merge-pi.log")
        try:
            attempt, outcome, signal = self._attempt(
                pi, rung, message, events_dir / f"{task.task_id}-merge-{len(attempts) + 1}-{tier}.jsonl", "merge"
            )
        except PiError as error:
            return f"resolving the conflict failed: {error}"
        finally:
            pi.close()
        attempts.append(attempt)
        if attempt.stop_reason != "settled":
            return f"resolving the conflict stopped ({attempt.stop_reason}): {outcome.error or (signal.detail if signal else '')}"
        test = attempt.test_result = self.check(task, Workspace(task.task_id, path, "", ""))
        if test.passed is False:
            return f"tests fail after resolving the conflict: {test.detail.splitlines()[0] if test.detail else ''}"
        return None

    def _report(self, task: RunTask, start: Tier | None, explored: bool, **fields: Any) -> ExecutionReport:
        failure = fields.pop("failure", None)
        detail = fields.pop("detail", "")
        return ExecutionReport(
            run_id=self.run_id,
            project_id=self.project_id,
            task_id=task.task_id,
            source=task.source,
            title=task.title,
            predicted_tier=task.predicted_tier,
            predicted_features=task.predicted_features,
            predicted_score=task.predicted_score,
            start_tier=start,
            explored=explored,
            solved_tier=fields.pop("solved_tier", None),
            failure_category=failure,
            failure_detail=detail,
            handoff=self.config.handoff,
            test_command=self.test_command(task),
            depends_on=task.depends_on,
            acceptance=task.acceptance,
            config_hash=self.config_hash,
            runner_version=__version__,
            pi_version=self.pi_version,
            **fields,
        )

    def run(
        self, task: RunTask, workspace: Workspace, start: Tier, explored: bool = False, run_index: int = 0,
        merge_attempts: list[Attempt] | None = None,
    ) -> ExecutionReport:
        events_dir = self.run_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        attempts: list[Attempt] = list(merge_attempts or [])
        escalations: list[Escalation] = []
        tier: Tier = start
        pi: PiClient | None = None
        note: str | None = None
        solved: Tier | None = None
        last_test: TestResult | None = None
        failure: FailureCategory | None = None
        detail = ""
        started = time.monotonic()
        prefix = task.task_id if run_index == 0 else f"{task.task_id}.r{run_index}"
        try:
            while True:
                rung = self.config.rung(tier)
                index = sum(1 for a in attempts if a.purpose == "task") + 1
                if pi is None:
                    pi = self.pi_factory(rung, workspace.path, self.run_dir / "logs" / f"{prefix}-pi.log")
                    message = task_message(task, self.test_command(task))
                elif self.config.handoff == "continue":
                    pi.set_model(rung)
                    message = note or ""
                else:
                    if self.config.handoff == "restart":
                        self.workspaces.reset(workspace)
                    pi.new_session()
                    pi.set_model(rung)
                    message = task_message(task, self.test_command(task))
                    if self.config.handoff == "discard" and note:
                        message = f"{message}\n\n---\n{note}"
                path = events_dir / f"{prefix}-attempt-{index}-{tier}.jsonl"
                attempt, outcome, watch_signal = self._attempt(pi, rung, message, path)
                stop = attempt.stop_reason
                attempts.append(attempt)
                if stop == "error" or stop == "timeout":
                    failure, detail = "error", outcome.error or "pi run timed out"
                    break
                signal = watch_signal
                if stop == "settled":
                    last_test = attempt.test_result = self.check(task, workspace)
                    if last_test.passed is not False:
                        solved = tier if last_test.passed else None
                        break
                    signal = Signal("tests_failed", last_test.detail.splitlines()[0], attempt.steps)
                assert signal is not None
                upper = self.config.above(tier)
                if upper is None:
                    failure = (
                        "tests_failed" if signal.name == "tests_failed"
                        else "budget_exhausted" if signal.name in ("step_budget", "cost_budget")
                        else "signal_on_top_tier"
                    )  # fmt: skip
                    detail = signal.detail
                    if last_test is None:
                        last_test = self.check(task, workspace)
                    break
                escalations.append(Escalation(from_tier=tier, to_tier=upper, step=signal.step, signal=signal.name, detail=signal.detail))
                note = HANDOFF_NOTE.format(detail=signal.detail, diffstat=self.workspaces.working_diff(workspace) or "(none)")
                tier = upper
        except PiError as error:
            failure, detail = "error", str(error)
        finally:
            if pi is not None:
                pi.close()

        commit = self.workspaces.commit(workspace, f"KM0 task {task.task_id}: {task.title}".strip())
        diff_path = None
        if commit:
            diff_path = self.run_dir / "diffs" / f"{prefix}.diff"
            diff_path.parent.mkdir(parents=True, exist_ok=True)
            diff_path.write_text(self.workspaces.diff(workspace, commit), encoding="utf-8")
        merge_diff = None
        if workspace.resolved:
            merge_diff = self.run_dir / "diffs" / f"{prefix}.merge.diff"
            merge_diff.parent.mkdir(parents=True, exist_ok=True)
            merge_diff.write_text(self.workspaces.merge_diff(workspace), encoding="utf-8")
        total = Tokens()
        for attempt in attempts:
            total = total + attempt.tokens
        return self._report(
            task,
            start=start,
            explored=explored,
            solved_tier=solved,
            escalations=escalations,
            cost=round(sum(a.cost for a in attempts), 6),
            steps=sum(a.steps for a in attempts),
            tokens=total,
            wall_time=round(time.monotonic() - started, 2),
            test_result=last_test,
            failure=failure,
            detail=detail,
            branch=workspace.branch,
            base_commit=workspace.base_commit,
            commit=commit,
            diff=str(diff_path.relative_to(self.run_dir)) if diff_path else None,
            run_index=run_index,
            merge_resolved=workspace.resolved,
            merge_diff=str(merge_diff.relative_to(self.run_dir)) if merge_diff else None,
            attempts=attempts,
        )
