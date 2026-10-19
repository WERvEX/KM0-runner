"""The tier ladder: which model runs each tier, with what budget, and how runs are configured."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from . import __version__

Tier = Literal["T1", "T2", "T3"]
TIERS: tuple[Tier, ...] = ("T1", "T2", "T3")

HandoffMode = Literal["discard", "full", "restart"]
"""How a higher tier takes over after an escalation.

- discard: drop the lower tier's trajectory, keep the repository as it is, pass a short note (default).
- full: keep the repository and pass the lower tier's whole trajectory as context.
- restart: reset the repository and start the higher tier from scratch.
"""


def next_tier(tier: Tier) -> Tier | None:
    index = TIERS.index(tier)
    return TIERS[index + 1] if index + 1 < len(TIERS) else None


class TierModel(BaseModel):
    """One rung of the ladder. `model_*` fields are passed to mini-swe-agent's `get_model`."""

    model_name: str
    model_class: str = ""
    model_kwargs: dict[str, Any] = Field(default_factory=dict)
    step_limit: int = 50
    """Steps this tier may take before the step-budget signal escalates it."""
    cost_limit: float = 3.0
    """USD this tier may spend on one task; mini-swe-agent stops the run past it."""


class SignalConfig(BaseModel):
    """Thresholds for the trajectory signals. Starting guesses, to be tuned on the dev set."""

    test_fail_streak: int = 3
    """Test runs in a row that fail."""
    same_file_edits: int = 5
    """Edits to one file within one attempt."""
    repeat_command: int = 3
    """The same command run this many times in a row."""
    escalate_on_failed_submit: bool = True
    """Escalate when the agent submits but the judge says the tests fail."""


class RunnerConfig(BaseModel):
    tiers: dict[Tier, TierModel]
    signals: SignalConfig = Field(default_factory=SignalConfig)
    handoff: HandoffMode = "discard"
    agent_config: str = "default.yaml"
    """mini-swe-agent config the agent templates come from: a builtin name or a path."""
    environment: dict[str, Any] = Field(default_factory=dict)
    """Merged over the agent config's `environment` section."""
    reset_command: str = "git reset --hard -q && git clean -fdq"
    """Run in the environment's working directory before a `restart` handoff."""
    trajectory_note_chars: int = 60_000
    """Cap on the lower tier's transcript passed in a `full` handoff."""

    def tier(self, tier: Tier) -> TierModel:
        if tier not in self.tiers:
            raise KeyError(f"no model configured for {tier}")
        return self.tiers[tier]

    def start_tier(self, wanted: Tier) -> Tier:
        """The lowest configured tier at or above `wanted`, else the top one: a T3-only ladder runs all on T3."""
        configured = sorted(self.tiers, key=TIERS.index)
        return next((t for t in configured if TIERS.index(t) >= TIERS.index(wanted)), configured[-1])

    def above(self, tier: Tier) -> Tier | None:
        """The next configured tier up; None on the top one."""
        upper = next_tier(tier)
        while upper and upper not in self.tiers:
            upper = next_tier(upper)
        return upper

    def hash(self) -> str:
        """Hash of everything that changes how a run behaves, so a report can be reproduced."""
        from minisweagent import __version__ as mini_version

        payload = {"config": self.model_dump(mode="json"), "runner": __version__, "mini": mini_version}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return digest[:16]


def load_config(path: str | Path) -> RunnerConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return RunnerConfig.model_validate(data)
