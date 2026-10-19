"""Aggregate execution reports: the cost–solve-rate point of one run and KM0's prediction matrix."""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from pydantic import BaseModel

from .report import ExecutionReport
from .stats import bootstrap, by_task, mean, task_means, unstable_tasks
from .tiers import TIERS


class Summary(BaseModel):
    name: str = ""
    tasks: int
    runs: int
    """Reports; more than tasks when tasks were repeated."""
    judged: int
    solved: int
    solve_rate: float | None
    """Mean over judged tasks of the share of their repeats solved."""
    solve_rate_ci: tuple[float, float] | None
    mean_cost: float
    """Mean over tasks of their mean cost per run."""
    mean_cost_ci: tuple[float, float] | None
    total_cost: float
    mean_steps: float
    escalated: int
    unstable: list[str]
    """Tasks whose repeats disagree."""
    failures: dict[str, int]
    config_hashes: list[str]
    confusion: dict[str, dict[str, int]]
    """predicted tier → solved tier (or `unsolved`) → runs; KM0-routed, judged runs only."""


def load_reports(paths: list[str | Path]) -> list[ExecutionReport]:
    files: list[Path] = []
    for path in map(Path, paths):
        if path.is_dir():
            # A run directory (reports/NN.json), or a folder of them.
            files.extend(sorted(path.glob("reports/*.json")) or sorted(path.glob("*/reports/*.json")))
        else:
            files.append(path)
    return [ExecutionReport.model_validate_json(file.read_text(encoding="utf-8")) for file in files]


def summarize(reports: list[ExecutionReport], name: str = "") -> Summary:
    judged = [r for r in reports if r.test_result and r.test_result.passed is not None]
    solved = [r for r in reports if r.solved_tier]
    confusion = {tier: {column: 0 for column in (*TIERS, "unsolved")} for tier in TIERS}
    for report in judged:
        if report.predicted_tier:
            row = confusion.setdefault(report.predicted_tier, {})
            column = report.solved_tier or "unsolved"
            row[column] = row.get(column, 0) + 1
    solve_rates, costs = task_means(reports)
    count = len(reports) or 1
    return Summary(
        name=name,
        tasks=len(by_task(reports)),
        runs=len(reports),
        judged=len(judged),
        solved=len(solved),
        solve_rate=round(mean(solve_rates), 4) if solve_rates else None,
        solve_rate_ci=bootstrap(solve_rates),
        mean_cost=round(mean(costs), 6),
        mean_cost_ci=bootstrap(costs),
        total_cost=round(sum(r.cost for r in reports), 6),
        mean_steps=round(sum(r.steps for r in reports) / count, 2),
        escalated=sum(1 for r in reports if r.escalations),
        unstable=unstable_tasks(reports),
        failures=dict(Counter(r.failure_category for r in reports if r.failure_category)),
        config_hashes=sorted({r.config_hash for r in reports}),
        confusion=confusion,
    )


def write_points(summaries: list[Summary], path: str | Path) -> None:
    """One row per run: the numbers behind the cost–solve-rate scatter."""
    with Path(path).open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["run", "tasks", "mean_cost", "cost_low", "cost_high", "solve_rate", "solve_low", "solve_high"])
        for s in summaries:
            cost_ci = s.mean_cost_ci or (None, None)
            solve_ci = s.solve_rate_ci or (None, None)
            writer.writerow([s.name, s.tasks, s.mean_cost, *cost_ci, s.solve_rate, *solve_ci])


def plot(summaries: list[Summary], path: str | Path) -> None:
    """Cost–solve-rate scatter, one labelled point per run with 95% bootstrap whiskers. Needs matplotlib."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ink, muted, grid, mark = "#1f1f1e", "#6b6a64", "#e4e3dd", "#2a78d6"
    fig, ax = plt.subplots(figsize=(7, 4.5), dpi=150)
    for s in summaries:
        if s.solve_rate is None:
            continue
        xerr = [[s.mean_cost - s.mean_cost_ci[0]], [s.mean_cost_ci[1] - s.mean_cost]] if s.mean_cost_ci else None
        yerr = [[s.solve_rate - s.solve_rate_ci[0]], [s.solve_rate_ci[1] - s.solve_rate]] if s.solve_rate_ci else None
        ax.errorbar(
            s.mean_cost, s.solve_rate, xerr=xerr, yerr=yerr, fmt="o", color=mark, ecolor=mark,
            elinewidth=1, capsize=0, markersize=7, markeredgecolor="white", markeredgewidth=1.5,
        )  # fmt: skip
        ax.annotate(s.name, (s.mean_cost, s.solve_rate), xytext=(8, 6), textcoords="offset points", color=ink, fontsize=9)
    ax.set_xlabel("Mean cost per task (USD)", color=muted)
    ax.set_ylabel("Solve rate", color=muted)
    ax.set_title("Cost vs solve rate (95% bootstrap intervals)", color=ink, fontsize=11, loc="left")
    ax.set_ylim(0, 1.05)
    ax.set_xlim(left=0)
    ax.grid(True, color=grid, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(grid)
    ax.tick_params(colors=muted)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
