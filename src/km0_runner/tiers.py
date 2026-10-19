"""Runner configuration: pi, the ladder of model tiers, signals, handoff, exploration and isolation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from . import __version__

Tier = str
"""A rung name on the ladder, such as `T2`."""
KM0_TIERS: tuple[str, ...] = ("T1", "T2", "T3")
"""The tiers KM0 predicts; `km0_tiers` maps them onto the ladder."""
TIERS = KM0_TIERS

HandoffMode = Literal["discard", "continue", "restart"]
"""How a higher tier takes over after an escalation.

- discard: a new pi session on the stronger model, the repository as it is, and a short handoff note (default).
- continue: switch the model inside the same session and send the note.
- restart: reset the workspace to its starting point and begin a new session from scratch.
"""


class PiConfig(BaseModel):
    command: list[str] = Field(default_factory=lambda: ["pi"])
    """How to start pi, such as `["npx", "-y", "@earendil-works/pi-coding-agent@1.3.0"]`."""
    version: str = "1.3.0"
    """The pinned version; the Runner refuses another one unless `check_version` is off."""
    check_version: bool = True
    tools: list[str] = Field(default_factory=lambda: ["read", "bash", "edit", "write"])
    agent_dir: str | None = None
    """pi's agent directory (`PI_CODING_AGENT_DIR`), holding `models.json` with the providers. None: pi's default."""
    models_json: str | None = None
    """A `models.json` to use instead; the Runner gives pi a private agent directory holding only this file."""
    extra_args: list[str] = Field(default_factory=list)
    turn_timeout: float = 3600
    """Seconds one prompt may run before the Runner aborts it."""


class Rung(BaseModel):
    """One tier of the ladder."""

    name: Tier
    model: str
    """pi's `provider/modelId`; a relay is configured as a provider in pi's `models.json`."""
    step_limit: int = 50
    """Assistant turns this tier may take on one task before it is escalated."""
    cost_limit: float = 3.0
    """USD this tier may spend on one task before it is escalated."""

    @property
    def provider(self) -> str:
        return self.model.split("/", 1)[0]

    @property
    def model_id(self) -> str:
        return self.model.split("/", 1)[1]


class SignalConfig(BaseModel):
    """Thresholds for the trajectory signals. Starting guesses, to be tuned on real runs."""

    test_fail_streak: int = 3
    """Test runs in a row that fail."""
    same_file_edits: int = 5
    """Edits to one file within one attempt."""
    repeat_command: int = 3
    """The same command run this many times in a row."""
    tool_error_streak: int = 5
    """Tool calls in a row that return an error."""


class Exploration(BaseModel):
    rate: float = 0.0
    """Share of tasks started one tier below KM0's prediction, to find tasks that need less."""
    only_low_risk: bool = True
    """Never explore a task KM0 marked with a risk."""
    seed: int = 0


class RunnerConfig(BaseModel):
    pi: PiConfig = Field(default_factory=PiConfig)
    ladder: list[Rung]
    """Ordered from cheapest to strongest."""
    km0_tiers: dict[str, Tier] = Field(default_factory=dict)
    """KM0 tier → rung to start on. Missing entries map to the rung of the same name, else the nearest above."""
    signals: SignalConfig = Field(default_factory=SignalConfig)
    handoff: HandoffMode = "discard"
    exploration: Exploration = Field(default_factory=Exploration)
    isolation: Literal["worktree", "docker"] = "worktree"
    docker_image: str | None = None
    """Image with node and pi for `isolation: docker`; the workspace is mounted at /workspace."""
    test_command: str | None = None
    """Used when a task carries no `testCommand`."""
    test_timeout: int = 900

    @model_validator(mode="after")
    def _check(self) -> RunnerConfig:
        names = [rung.name for rung in self.ladder]
        if not names or len(set(names)) != len(names):
            raise ValueError("ladder needs at least one rung and unique names")
        for rung in self.ladder:
            if "/" not in rung.model:
                raise ValueError(f"rung {rung.name}: model must be provider/modelId, got {rung.model!r}")
        for km0, rung in self.km0_tiers.items():
            if rung not in names:
                raise ValueError(f"km0_tiers maps {km0} to unknown rung {rung}")
        if self.isolation == "docker" and not self.docker_image:
            raise ValueError("isolation: docker needs docker_image")
        return self

    @property
    def names(self) -> list[Tier]:
        return [rung.name for rung in self.ladder]

    def rung(self, name: Tier) -> Rung:
        for rung in self.ladder:
            if rung.name == name:
                return rung
        raise KeyError(f"no rung named {name}")

    def index(self, name: Tier) -> int:
        return self.names.index(name)

    def start_rung(self, km0_tier: str | None) -> Tier:
        """The rung a KM0 tier starts on; no prediction starts at the bottom."""
        if km0_tier is None:
            return self.names[0]
        if km0_tier in self.km0_tiers:
            return self.km0_tiers[km0_tier]
        if km0_tier in self.names:
            return km0_tier
        if km0_tier in KM0_TIERS:
            # A ladder without that name: the lowest rung at or above its position among KM0's tiers.
            position = KM0_TIERS.index(km0_tier)
            return self.names[min(position, len(self.names) - 1)]
        raise KeyError(f"cannot place KM0 tier {km0_tier} on the ladder")

    def above(self, name: Tier) -> Tier | None:
        index = self.index(name)
        return self.names[index + 1] if index + 1 < len(self.names) else None

    def below(self, name: Tier) -> Tier | None:
        index = self.index(name)
        return self.names[index - 1] if index > 0 else None

    def hash(self, pi_version: str | None = None, prompt: str = "") -> str:
        """Hash of everything that changes how a run behaves: config, Runner and pi versions, Runner prompt."""
        payload = {
            "config": self.model_dump(mode="json"),
            "runner": __version__,
            "pi": pi_version or self.pi.version,
            "prompt": prompt,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def load_config(path: str | Path) -> RunnerConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return RunnerConfig.model_validate(data)
