"""Uncertainty for run summaries: task-level bootstrap intervals and repeat-run variance."""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Sequence

from .report import ExecutionReport


def by_task(reports: list[ExecutionReport]) -> dict[str, list[ExecutionReport]]:
    """Reports grouped by task: one entry per task, one report per repeat run."""
    grouped: dict[str, list[ExecutionReport]] = defaultdict(list)
    for report in reports:
        grouped[report.task_id].append(report)
    return dict(grouped)


def task_means(reports: list[ExecutionReport]) -> tuple[list[float], list[float]]:
    """Per task: share of judged repeats solved, and mean cost. Unjudged tasks drop out of solve rates."""
    solved, cost = [], []
    for runs in by_task(reports).values():
        judged = [r for r in runs if r.test_result and r.test_result.passed is not None]
        if judged:
            solved.append(sum(1 for r in judged if r.solved_tier) / len(judged))
        cost.append(sum(r.cost for r in runs) / len(runs))
    return solved, cost


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def bootstrap(
    values: Sequence[float],
    statistic: Callable[[Sequence[float]], float] = mean,
    resamples: int = 2000,
    level: float = 0.95,
    seed: int = 0,
) -> tuple[float, float] | None:
    """Percentile interval of `statistic` over tasks resampled with replacement; None under 2 tasks."""
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    n = len(values)
    stats = sorted(statistic([values[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples))
    tail = (1 - level) / 2
    low = stats[int(tail * resamples)]
    high = stats[min(resamples - 1, int((1 - tail) * resamples))]
    return round(low, 6), round(high, 6)


def unstable_tasks(reports: list[ExecutionReport]) -> list[str]:
    """Tasks whose repeat runs disagree on solved or not."""
    out = []
    for task_id, runs in by_task(reports).items():
        outcomes = {bool(r.solved_tier) for r in runs if r.test_result and r.test_result.passed is not None}
        if len(outcomes) > 1:
            out.append(task_id)
    return sorted(out)
