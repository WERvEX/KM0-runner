"""Aggregate execution reports: the cost–solve-rate point of one run and KM0's prediction matrix."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from pydantic import BaseModel

from .report import ExecutionReport
from .tiers import TIERS


class Summary(BaseModel):
    tasks: int
    judged: int
    solved: int
    solve_rate: float | None
    """Solved over judged tasks."""
    mean_cost: float
    total_cost: float
    mean_steps: float
    escalated: int
    failures: dict[str, int]
    config_hashes: list[str]
    confusion: dict[str, dict[str, int]]
    """predicted tier → solved tier (or `unsolved`) → count; KM0-routed tasks only."""


def load_reports(paths: list[str | Path]) -> list[ExecutionReport]:
    files: list[Path] = []
    for path in map(Path, paths):
        files.extend(sorted(path.glob("*/report.json")) if path.is_dir() else [path])
    return [ExecutionReport.model_validate_json(file.read_text(encoding="utf-8")) for file in files]


def summarize(reports: list[ExecutionReport]) -> Summary:
    judged = [r for r in reports if r.test_result and r.test_result.passed is not None]
    solved = [r for r in reports if r.solved_tier]
    confusion = {tier: {column: 0 for column in (*TIERS, "unsolved")} for tier in TIERS}
    for report in judged:
        if report.predicted_tier:
            confusion[report.predicted_tier][report.solved_tier or "unsolved"] += 1
    count = len(reports) or 1
    return Summary(
        tasks=len(reports),
        judged=len(judged),
        solved=len(solved),
        solve_rate=round(len(solved) / len(judged), 4) if judged else None,
        mean_cost=round(sum(r.cost for r in reports) / count, 6),
        total_cost=round(sum(r.cost for r in reports), 6),
        mean_steps=round(sum(r.steps for r in reports) / count, 2),
        escalated=sum(1 for r in reports if r.escalations),
        failures=dict(Counter(r.failure_category for r in reports if r.failure_category)),
        config_hashes=sorted({r.config_hash for r in reports}),
        confusion=confusion,
    )
