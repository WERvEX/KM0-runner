"""One run of a KM0 package: order the tasks, give each a workspace, run the cascade, write reports and a summary.

    runs/<run_id>/
      run.json                  config hash, pi version, start commit, task order
      reports/NN.json           one execution report per task (report_version 2)
      diffs/NN.diff             each task's change, for KM0's acceptance
      events/NN-attempt-K-T1.jsonl  pi's events per attempt
      logs/NN-pi.log            pi's stderr
      summary.json              cost, tier use, pass rate, and the all-top-tier estimate
      progress.jsonl            live events for KM0 (see progress.py)
      STOP                      created by whoever wants the run to stop
      acceptance.jsonl          written later by KM0
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from .cascade import Cascade, PiFactory
from .handoff import Package, RunTask
from .pi_client import PiClient, agent_dir, pi_argv
from .progress import NEVER, PROGRESS_FILE, Progress, StopRequest
from .report import Attempt, ExecutionReport
from .scheduler import order, start_tier
from .tiers import RunnerConfig, Rung, Tier
from .workspace import MergeConflict, Workspaces


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def default_pi_factory(config: RunnerConfig, scratch: Path) -> PiFactory:
    agent = agent_dir(config.pi, scratch)

    def start(rung: Rung, workspace: Path, log: Path) -> PiClient:
        log.parent.mkdir(parents=True, exist_ok=True)
        image = config.docker_image if config.isolation == "docker" else None
        argv, env = pi_argv(config.pi, rung, workspace, agent, image)
        return PiClient(argv, workspace, env, log).start()

    return start


class TierUse(BaseModel):
    attempts: int = 0
    cost: float = 0.0
    solved: int = 0


class RunSummary(BaseModel):
    run_id: str
    project_id: int | str | None
    tasks: int
    passed: int
    """Tasks whose test command passed."""
    unjudged: int
    """Tasks without a test command: KM0 decides."""
    failed: int
    blocked: int
    cancelled: int = 0
    """Tasks the run was stopped during or before."""
    pass_rate: float | None
    """passed / tasks that had a test command and ran."""
    cost: float
    tiers: dict[Tier, TierUse]
    top_tier_estimate: float | None
    """The same tokens priced at the top rung's rates: an estimate of running everything on the strongest model."""
    escalations: int
    explored: int
    wall_time: float


def load_prices(config: RunnerConfig, scratch: Path) -> dict[str, dict[str, float]]:
    """`provider/modelId` → USD per million tokens, from pi's `models.json` when it has them."""
    directory = agent_dir(config.pi, scratch)
    path = Path(directory) / "models.json" if directory else Path.home() / ".pi" / "agent" / "models.json"
    if not path.exists():
        return {}
    prices: dict[str, dict[str, float]] = {}
    for provider, spec in (json.loads(path.read_text(encoding="utf-8")).get("providers") or {}).items():
        for model in spec.get("models") or []:
            if model.get("cost"):
                prices[f"{provider}/{model['id']}"] = model["cost"]
    return prices


def top_tier_estimate(reports: list[ExecutionReport], price: dict[str, float] | None) -> float | None:
    if not price:
        return None
    total = 0.0
    for report in reports:
        tokens = report.tokens
        total += (
            tokens.input * price.get("input", 0) + tokens.output * price.get("output", 0)
            + tokens.cache_read * price.get("cacheRead", 0) + tokens.cache_write * price.get("cacheWrite", 0)
        ) / 1e6  # fmt: skip
    return round(total, 6)


def summarize_run(
    run_id: str, project_id: int | str | None, reports: list[ExecutionReport], config: RunnerConfig,
    prices: dict[str, dict[str, float]], wall_time: float,
) -> RunSummary:  # fmt: skip
    tiers = {name: TierUse() for name in config.names}
    for report in reports:
        for attempt in report.attempts:
            use = tiers.setdefault(attempt.tier, TierUse())
            use.attempts += 1
            use.cost = round(use.cost + attempt.cost, 6)
        if report.solved_tier:
            tiers.setdefault(report.solved_tier, TierUse()).solved += 1
    judged = [r for r in reports if r.test_result and r.test_result.passed is not None and r.failure_category != "blocked"]
    passed = sum(1 for r in judged if r.test_result and r.test_result.passed)
    return RunSummary(
        run_id=run_id,
        project_id=project_id,
        tasks=len(reports),
        passed=passed,
        unjudged=sum(1 for r in reports if r.failure_category is None and r.solved_tier is None),
        failed=sum(1 for r in reports if r.failure_category not in (None, "blocked", "cancelled")),
        blocked=sum(1 for r in reports if r.failure_category == "blocked"),
        cancelled=sum(1 for r in reports if r.failure_category == "cancelled"),
        pass_rate=round(passed / len(judged), 4) if judged else None,
        cost=round(sum(r.cost for r in reports), 6),
        tiers=tiers,
        top_tier_estimate=top_tier_estimate(reports, prices.get(config.ladder[-1].model)),
        escalations=sum(len(r.escalations) for r in reports),
        explored=sum(1 for r in reports if r.explored),
        wall_time=round(wall_time, 2),
    )


def task_status(report: ExecutionReport) -> str:
    """How a task ended, as `progress.jsonl` names it."""
    if report.failure_category in ("blocked", "cancelled"):
        return report.failure_category
    if report.failure_category is not None:
        return "failed"
    return "passed" if report.solved_tier else "unjudged"


def run_package(
    package: Package,
    repo: str | Path,
    config: RunnerConfig,
    runs_dir: str | Path = "runs",
    run_id: str | None = None,
    pi_factory: PiFactory | None = None,
    pi_version: str | None = None,
    only: list[str] | None = None,
    start: Tier | None = None,
    log: Callable[[str], None] = print,
    reuse: str | None = None,
    stop: StopRequest = NEVER,
) -> tuple[Path, list[ExecutionReport]]:
    """Run the package's tasks (or `only` those) in dependency order, writing `progress.jsonl` as it goes.

    With `reuse`, upstream tasks left out of this run start from the branches run `reuse` made for them, so a
    task KM0 rejected can be redone on top of the ones it accepted. When `stop` is requested, the current task
    is stopped and it and every later task are reported as cancelled.
    """
    run_id = run_id or new_run_id()
    run_dir = Path(runs_dir).resolve() / run_id
    (run_dir / "reports").mkdir(parents=True, exist_ok=True)
    workspaces = Workspaces(repo, run_id, run_dir / "worktrees", package.root)
    factory = pi_factory or default_pi_factory(config, run_dir)
    progress = Progress(run_dir / PROGRESS_FILE)
    cascade = Cascade(
        config, run_dir, run_id, workspaces, factory, pi_version, project_id=package.project_id, progress=progress,
        stop=stop,
    )  # fmt: skip
    tasks = order(package.tasks)
    if only:
        tasks = [task for task in tasks if task.task_id in only]
    selected = {task.task_id for task in tasks}
    reused = workspaces.reuse(reuse, [t.task_id for t in package.tasks if t.task_id not in selected]) if reuse else []
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id, "project_id": package.project_id, "repo": str(Path(repo).resolve()),
                "start_commit": workspaces.start_commit, "config_hash": cascade.config_hash,
                "pi_version": pi_version, "tasks": [task.task_id for task in tasks],
                "reused": {task_id: workspaces.branch(task_id) for task_id in reused},
                "config": config.model_dump(mode="json"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )  # fmt: skip
    progress.emit(
        "run_started", run_id=run_id, project_id=package.project_id,
        tasks=[{"task_id": task.task_id, "title": task.title} for task in tasks],
        ladder=[{"name": rung.name, "model": rung.model} for rung in config.ladder],
    )  # fmt: skip
    started = time.monotonic()
    reports: dict[str, ExecutionReport] = {}
    for task in tasks:
        if stop.requested:
            report = cascade.cancelled(task)
            reports[task.task_id] = report
            (run_dir / "reports" / f"{task.task_id}.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
            progress.emit("task_finished", task_id=task.task_id, status="cancelled", solved_tier=None, cost=0.0,
                          failure_category="cancelled", failure_detail=report.failure_detail)  # fmt: skip
            continue
        failed_upstream = [dep for dep in task.depends_on if dep in reports and reports[dep].failure_category is not None]
        missing = [dep for dep in task.depends_on if dep not in reports and dep not in workspaces.reused]
        if failed_upstream or missing:
            report = cascade.blocked(task, f"upstream tasks failed or did not run: {', '.join(failed_upstream + missing)}")
        else:
            tier, explored = start_tier(config, task, run_id, start)
            merge_attempts: list[Attempt] = []

            def resolve(
                path: Path, files: list[str], branch: str, task: RunTask = task, tier: Tier = tier,
                attempts: list[Attempt] = merge_attempts,
            ) -> str | None:
                log(f"{task.task_id}: upstream branches conflict in {', '.join(files)}; resolving on {tier}")
                return cascade.resolve_merge(task, tier, path, files, branch, attempts)

            try:
                workspace = workspaces.create(task.task_id, task.depends_on, resolve)
            except MergeConflict as conflict:
                report = cascade.blocked(task, str(conflict), merge_attempts)
            else:
                log(f"{task.task_id} {task.title}: starting on {tier}{' (exploring)' if explored else ''}")
                progress.emit("task_started", task_id=task.task_id, title=task.title, tier=tier, explored=explored)
                try:
                    report = cascade.run(task, workspace, tier, explored, merge_attempts=merge_attempts)
                finally:
                    workspaces.remove(workspace)
        reports[task.task_id] = report
        (run_dir / "reports" / f"{task.task_id}.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
        progress.emit(
            "task_finished", task_id=task.task_id, status=task_status(report), solved_tier=report.solved_tier,
            cost=report.cost, failure_category=report.failure_category, failure_detail=report.failure_detail[-500:],
        )  # fmt: skip
        log(
            f"{task.task_id}: start={report.start_tier} solved={report.solved_tier} "
            f"escalations={len(report.escalations)} cost=${report.cost:.4f} failure={report.failure_category}"
        )
    summary = summarize_run(
        run_id, package.project_id, list(reports.values()), config, load_prices(config, run_dir), time.monotonic() - started
    )
    (run_dir / "summary.json").write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    progress.emit(
        "run_finished", status="cancelled" if stop.requested else "completed", passed=summary.passed,
        unjudged=summary.unjudged, failed=summary.failed, blocked=summary.blocked, cancelled=summary.cancelled,
        cost=summary.cost,
    )  # fmt: skip
    progress.close()
    return run_dir, list(reports.values())
