"""Calibrate KM0's routing rules against execution reports: predicted tier vs the tier that solved the task.

The objective is the expected cost of the cascade under the rules, from measured per-tier costs: a task
started on tier p that needs tier a pays for every tier from p up to a; started above a, it pays for p
alone. Unsolved tasks need more than the top tier, so they pay for every tier from p up. Accuracy
(predicted == needed) is reported alongside. The search changes the tier limits first, then each feature
value's points by coordinate descent; hard rules stay fixed. Calibrate on dev, check on test.
"""

from __future__ import annotations

import copy
import itertools
from collections import defaultdict

from pydantic import BaseModel

from .report import ExecutionReport
from .scoring import RoutingRules, TaskFeatures, decide_tier
from .tiers import TIERS, Tier

POINT_RANGE = range(0, 5)
LIMIT_RANGE = range(0, 13)


class Sample(BaseModel):
    task_id: str
    features: TaskFeatures
    needed: Tier | None
    """Tier that solved it; None when no tier did."""


class Evaluation(BaseModel):
    tasks: int
    accuracy: float
    under: int
    """Predicted below the tier that was needed: paid for escalations."""
    over: int
    """Predicted above it: paid a stronger model than needed."""
    expected_cost: float
    confusion: dict[str, dict[str, int]]


class Calibration(BaseModel):
    rules: RoutingRules
    tier_costs: dict[str, float]
    dev_before: Evaluation
    dev_after: Evaluation
    test_before: Evaluation | None = None
    test_after: Evaluation | None = None


def samples(reports: list[ExecutionReport]) -> list[Sample]:
    """Judged reports that carry KM0's features and started on T1; others cannot be replayed.

    A cascade that started higher never tried the tiers below its start, so its solved tier only bounds
    the needed tier from above. Run calibration data with `--start-tier T1`.
    """
    out = []
    for report in reports:
        if report.predicted_features is None or not report.test_result or report.test_result.passed is None:
            continue
        if report.start_tier != "T1":
            continue
        out.append(
            Sample(
                task_id=report.task_id,
                features=TaskFeatures.model_validate(report.predicted_features),
                needed=report.solved_tier,
            )
        )
    return out


def tier_costs(reports: list[ExecutionReport], fallback: dict[str, float] | None = None) -> dict[str, float]:
    """Mean cost of one attempt per tier, measured from the reports; `fallback` fills tiers never run."""
    spent: dict[str, list[float]] = defaultdict(list)
    for report in reports:
        for attempt in report.attempts:
            spent[attempt.tier].append(attempt.cost)
    costs = {tier: sum(values) / len(values) for tier, values in spent.items() if values}
    defaults = fallback or {"T1": 1.0, "T2": 3.0, "T3": 10.0}
    return {tier: costs.get(tier) or defaults[tier] for tier in TIERS}


def task_cost(predicted: Tier, needed: Tier | None, costs: dict[str, float]) -> float:
    start = TIERS.index(predicted)
    end = TIERS.index(needed) if needed else len(TIERS) - 1
    return sum(costs[tier] for tier in TIERS[start : max(start, end) + 1])


def evaluate(rules: RoutingRules, data: list[Sample], costs: dict[str, float]) -> Evaluation:
    confusion = {tier: {column: 0 for column in (*TIERS, "unsolved")} for tier in TIERS}
    hits = under = over = 0
    total = 0.0
    for sample in data:
        predicted = decide_tier(sample.features, rules)
        confusion[predicted][sample.needed or "unsolved"] += 1
        total += task_cost(predicted, sample.needed, costs)
        needed = TIERS.index(sample.needed) if sample.needed else len(TIERS)
        hits += TIERS.index(predicted) == needed
        under += TIERS.index(predicted) < needed
        over += TIERS.index(predicted) > needed
    n = len(data) or 1
    return Evaluation(
        tasks=len(data),
        accuracy=round(hits / n, 4),
        under=under,
        over=over,
        expected_cost=round(total / n, 6),
        confusion=confusion,
    )


def _objective(rules: RoutingRules, data: list[Sample], costs: dict[str, float]) -> tuple[float, float]:
    result = evaluate(rules, data, costs)
    return (result.expected_cost, -result.accuracy)


def fit(data: list[Sample], costs: dict[str, float], start: RoutingRules | None = None, passes: int = 3) -> RoutingRules:
    best = copy.deepcopy(start or RoutingRules())
    best_value = _objective(best, data, costs)

    def try_rules(candidate: RoutingRules) -> None:
        nonlocal best, best_value
        value = _objective(candidate, data, costs)
        if value < best_value:
            best, best_value = candidate, value

    for _ in range(passes):
        for t1, t2 in itertools.product(LIMIT_RANGE, LIMIT_RANGE):
            if t1 <= t2:
                candidate = copy.deepcopy(best)
                candidate.tierLimits = {"T1": t1, "T2": t2}
                try_rules(candidate)
        for name, values in best.featurePoints.items():
            for value in values:
                for points in POINT_RANGE:
                    candidate = copy.deepcopy(best)
                    candidate.featurePoints[name][value] = points
                    try_rules(candidate)
    return best


def calibrate(
    dev: list[ExecutionReport], test: list[ExecutionReport] | None = None, start: RoutingRules | None = None
) -> Calibration:
    costs = tier_costs(dev + (test or []))
    dev_data = samples(dev)
    if not dev_data:
        raise ValueError("no judged reports with KM0 features in the dev set")
    before = start or RoutingRules()
    after = fit(dev_data, costs, before)
    result = Calibration(
        rules=after,
        tier_costs={tier: round(cost, 6) for tier, cost in costs.items()},
        dev_before=evaluate(before, dev_data, costs),
        dev_after=evaluate(after, dev_data, costs),
    )
    if test:
        test_data = samples(test)
        result.test_before = evaluate(before, test_data, costs)
        result.test_after = evaluate(after, test_data, costs)
    return result
