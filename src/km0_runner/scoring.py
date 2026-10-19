"""KM0's tier rules, ported from `packages/core/src/routing/scoring.ts` so calibration can replay them.

Keep this in step with KM0: same feature points, same limits, same hard rules in the same order.
`RoutingRules` is also the file format KM0 imports after calibration.
"""

from __future__ import annotations

import copy
from typing import Literal

from pydantic import BaseModel, Field

from .tiers import TIERS, Tier

Scope = Literal["single_file", "few_files", "many_files", "new_subsystem"]


class TaskFeatures(BaseModel):
    """KM0's `TaskFeatures`, as it appears in KM0's JSON (camelCase `hasTests`)."""

    scope: Scope
    ambiguity: Literal["low", "medium", "high"]
    reasoning: Literal["follow", "judgement", "design"]
    context: Literal["small", "large"]
    hasTests: bool
    risks: list[Literal["data_migration", "security", "production"]] = Field(default_factory=list)
    kind: Literal["code", "fix", "test", "docs", "boilerplate", "config"]


DEFAULT_POINTS: dict[str, dict[str, int]] = {
    "scope": {"single_file": 0, "few_files": 1, "many_files": 2, "new_subsystem": 3},
    "ambiguity": {"low": 0, "medium": 1, "high": 2},
    "reasoning": {"follow": 0, "judgement": 1, "design": 3},
    "context": {"small": 0, "large": 1},
    "hasTests": {"true": 0, "false": 1},
}
DEFAULT_LIMITS: dict[str, int] = {"T1": 2, "T2": 5}


class RoutingRules(BaseModel):
    """The tunable part of KM0's rules. Hard rules (docs → T1, risk/fix → ≥T2, design → T3) stay fixed."""

    featurePoints: dict[str, dict[str, int]] = Field(default_factory=lambda: copy.deepcopy(DEFAULT_POINTS))
    tierLimits: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_LIMITS))


def _value(features: TaskFeatures, name: str) -> str:
    value = getattr(features, name)
    return str(value).lower() if isinstance(value, bool) else value


def score(features: TaskFeatures, rules: RoutingRules) -> int:
    return sum(rules.featurePoints[name][_value(features, name)] for name in rules.featurePoints)


def _at_least(tier: Tier, floor: Tier) -> Tier:
    return tier if TIERS.index(tier) >= TIERS.index(floor) else floor


def decide_tier(features: TaskFeatures, rules: RoutingRules | None = None) -> Tier:
    rules = rules or RoutingRules()
    total = score(features, rules)
    tier: Tier = "T1" if total <= rules.tierLimits["T1"] else "T2" if total <= rules.tierLimits["T2"] else "T3"
    small = features.scope in ("single_file", "few_files")
    if features.kind in ("docs", "boilerplate") and small:
        tier = "T1"
    if features.risks:
        tier = _at_least(tier, "T2")
    if features.kind == "fix":
        tier = _at_least(tier, "T2")
    if features.reasoning == "design":
        tier = "T3"
    return tier
