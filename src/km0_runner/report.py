"""The execution report (v2): what the Runner did on one task, read back by KM0 for acceptance and calibration."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .tiers import HandoffMode, Tier

REPORT_VERSION = 2

FailureCategory = Literal[
    "tests_failed",  # the top tier finished, but the test command fails
    "budget_exhausted",  # the top tier ran out of steps or money
    "signal_on_top_tier",  # a trajectory signal fired on the top tier; nowhere left to escalate
    "blocked",  # an upstream task failed, or merging the upstream branches conflicted
    "error",  # the run crashed: pi, the model provider, or a Runner bug
]

StopReason = Literal["settled", "signal", "step_budget", "cost_budget", "timeout", "error"]


class Escalation(BaseModel):
    from_tier: Tier
    to_tier: Tier
    step: int
    """Turn of the lower tier's attempt at which it was stopped."""
    signal: str
    detail: str


class TestResult(BaseModel):
    __test__ = False  # not a pytest test class

    passed: bool | None
    """None when no test command was available."""
    judge: str
    detail: str = ""


class Tokens(BaseModel):
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def __add__(self, other: Tokens) -> Tokens:
        return Tokens(**{name: getattr(self, name) + getattr(other, name) for name in Tokens.model_fields})


class Attempt(BaseModel):
    purpose: Literal["task", "merge"] = "task"
    """`merge`: resolving a conflict between upstream branches before the task started."""
    tier: Tier
    model: str
    steps: int
    tokens: Tokens = Field(default_factory=Tokens)
    cost: float
    wall_time: float
    stop_reason: StopReason
    signal: str | None = None
    test_result: TestResult | None = None
    events: str | None = None
    """pi's event stream for this attempt, relative to the run directory."""


class ExecutionReport(BaseModel):
    report_version: int = REPORT_VERSION
    run_id: str
    project_id: int | str | None = None
    """The KM0 project the package came from."""
    task_id: str
    """KM0 task seq (`NN`), or SWE-bench instance id."""
    source: Literal["km0", "swebench"] = "km0"
    title: str = ""
    predicted_tier: str | None
    """KM0's tier; None when the task was not routed by KM0."""
    predicted_features: dict[str, Any] | None = None
    """KM0's task features behind that tier, for calibration."""
    predicted_score: int | None = None
    start_tier: Tier | None
    """None when the task never started (blocked)."""
    explored: bool = False
    """Started one tier below the prediction to probe whether a cheaper tier suffices."""
    solved_tier: Tier | None
    """Tier on which the Runner's test command passed. Whether the task is solved is KM0's acceptance."""
    escalations: list[Escalation] = Field(default_factory=list)
    cost: float = 0.0
    steps: int = 0
    tokens: Tokens = Field(default_factory=Tokens)
    wall_time: float = 0.0
    test_command: str | None = None
    test_result: TestResult | None = None
    failure_category: FailureCategory | None = None
    failure_detail: str = ""
    handoff: HandoffMode
    branch: str | None = None
    base_commit: str | None = None
    commit: str | None = None
    """Commit on `branch` holding the task's changes; None when nothing changed."""
    diff: str | None = None
    """The change as a patch file, relative to the run directory."""
    depends_on: list[str] = Field(default_factory=list)
    merge_resolved: list[str] = Field(default_factory=list)
    """Files that conflicted between upstream branches and were resolved before the task started."""
    merge_diff: str | None = None
    """How those conflicts were resolved, as a patch file relative to the run directory."""
    acceptance: list[str] = Field(default_factory=list)
    """KM0's acceptance criteria, passed through for KM0's own acceptance."""
    config_hash: str
    runner_version: str
    pi_version: str | None = None
    run_index: int = 0
    """Which repeat of the task this is, when tasks run several times."""
    attempts: list[Attempt] = Field(default_factory=list)
