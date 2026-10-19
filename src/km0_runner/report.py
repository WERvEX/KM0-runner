"""The execution report: the one file KM0 reads back to calibrate its routing."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .tiers import HandoffMode, Tier

REPORT_VERSION = 1

FailureCategory = Literal[
    "tests_failed",  # submitted on the top tier, but the judge says the tests fail
    "budget_exhausted",  # the top tier ran out of steps or money
    "signal_on_top_tier",  # a trajectory signal fired on the top tier; nowhere left to escalate
    "no_submission",  # the agent stopped without submitting (format errors, an exit it chose)
    "error",  # the run crashed: environment, model API or Runner bug
]


class Escalation(BaseModel):
    from_tier: Tier
    to_tier: Tier
    step: int
    """Step of the lower tier's attempt at which it was stopped."""
    signal: str
    detail: str


class TestResult(BaseModel):
    __test__ = False  # not a pytest test class

    passed: bool | None
    """None when no judge ran."""
    judge: str
    detail: str = ""


class Attempt(BaseModel):
    tier: Tier
    model: str
    exit_status: str
    signal: str | None
    steps: int
    cost: float
    wall_time: float
    test_result: TestResult | None
    trajectory: str | None
    """Path of the mini-swe-agent trajectory, relative to the run directory."""


class ExecutionReport(BaseModel):
    report_version: int = REPORT_VERSION
    task_id: str
    """KM0 task seq, or SWE-bench instance id."""
    source: Literal["km0", "swebench"]
    title: str = ""
    predicted_tier: Tier | None
    """KM0's tier; None when the task was not routed by KM0."""
    start_tier: Tier
    solved_tier: Tier | None
    escalations: list[Escalation] = Field(default_factory=list)
    cost: float
    steps: int
    wall_time: float
    test_result: TestResult | None
    failure_category: FailureCategory | None
    handoff: HandoffMode
    config_hash: str
    runner_version: str
    attempts: list[Attempt] = Field(default_factory=list)
    patch: str = ""
    """`git diff` of the final repository state."""
