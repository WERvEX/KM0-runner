"""Trajectory signals: rules that watch an attempt step by step and say when to escalate."""

from __future__ import annotations

import re
import shlex
from collections import Counter
from dataclasses import dataclass, field

from .tiers import SignalConfig

TEST_COMMAND = re.compile(
    r"(?:^|[;&|(\n])\s*(?:\w+=\S*\s+)*(?:timeout\s+\S+\s+)?("
    r"pytest|py\.test|tox|nox|python[\d.]*\s+-m\s+(pytest|unittest)|python[\d.]*\s+\S*runtests\.py|"
    r"(npm|pnpm|yarn)\s+(run\s+)?test|vitest|jest|go\s+test|cargo\s+test|mvn\s+test|gradle\w*\s+test|make\s+test"
    r")(?=\s|$)"
)
# Test summaries that mean failure even when a pipe (`pytest | tail`) hides the exit code.
TEST_FAILED_OUTPUT = re.compile(
    r"\b\d+ (failed|errors?)\b|^FAILED |^ERROR |Tests:\s+\d+ failed|^--- FAIL|test result: FAILED", re.MULTILINE
)
# File targets of the edit commands a bash-only agent uses: `sed -i`, redirections, `tee`.
SED_INPLACE = re.compile(r"\bsed\s+(?:-\w*i\w*|--in-place)\b")
REDIRECT = re.compile(r"(?<![0-9&<>])>{1,2}\s*(?!&)([^\s;&|<>]+)")
TEE = re.compile(r"\btee\s+(?:-a\s+)?([^\s;&|<>]+)")


@dataclass(frozen=True)
class Signal:
    name: str
    """test_fail_streak · same_file_edits · repeat_command · step_budget · failed_submit."""
    detail: str
    step: int


def edited_files(command: str) -> list[str]:
    """Files a shell command writes to, as far as plain pattern matching can tell."""
    files: list[str] = []
    for match in REDIRECT.finditer(command):
        target = match.group(1).strip("'\"")
        if target and target != "/dev/null" and not target.startswith("/tmp/"):
            files.append(target)
    files.extend(match.group(1).strip("'\"") for match in TEE.finditer(command))
    for segment in re.split(r"&&|\|\||;|\n", command):
        if not SED_INPLACE.search(segment):
            continue
        try:
            words = shlex.split(segment)
        except ValueError:
            continue
        if words and not words[-1].startswith("-"):
            files.append(words[-1])
    return list(dict.fromkeys(files))


def is_test_command(command: str) -> bool:
    return bool(TEST_COMMAND.search(command))


def normalize(command: str) -> str:
    return " ".join(command.split())


@dataclass
class SignalMonitor:
    """Watches one attempt. `observe` is fed every executed action and returns a signal once one fires."""

    config: SignalConfig
    step_limit: int
    steps: int = 0
    fail_streak: int = 0
    edits: Counter = field(default_factory=Counter)
    last_command: str = ""
    repeat_count: int = 0

    def start_step(self) -> Signal | None:
        """Called before the model is queried; fires when the tier's step budget is spent."""
        if 0 < self.step_limit <= self.steps:
            return Signal("step_budget", f"used {self.steps} of {self.step_limit} steps", self.steps)
        self.steps += 1
        return None

    def observe(self, command: str, returncode: int | None, output: str = "") -> Signal | None:
        step = self.steps
        if is_test_command(command):
            failed = returncode not in (0, None) or bool(TEST_FAILED_OUTPUT.search(output))
            self.fail_streak = self.fail_streak + 1 if failed else 0
            if self.fail_streak >= self.config.test_fail_streak:
                return Signal("test_fail_streak", f"{self.fail_streak} failing test runs in a row", step)

        for path in edited_files(command):
            self.edits[path] += 1
            if self.edits[path] >= self.config.same_file_edits:
                return Signal("same_file_edits", f"edited {path} {self.edits[path]} times", step)

        current = normalize(command)
        self.repeat_count = self.repeat_count + 1 if current == self.last_command else 1
        self.last_command = current
        if self.repeat_count >= self.config.repeat_command:
            return Signal("repeat_command", f"ran the same command {self.repeat_count} times: {current[:120]}", step)
        return None
