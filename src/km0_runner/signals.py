"""Trajectory signals: rules that watch an attempt's tool calls and say when to escalate."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .tiers import SignalConfig

TEST_COMMAND = re.compile(
    r"(?:^|[;&|(\n])\s*(?:\w+=\S*\s+)*(?:timeout\s+\S+\s+)?("
    r"pytest|py\.test|tox|nox|python[\d.]*\s+-m\s+(pytest|unittest)|python[\d.]*\s+\S*runtests\.py|"
    r"(npm|pnpm|yarn|bun)\s+(run\s+)?test|vitest|jest|go\s+test|cargo\s+test|mvn\s+test|gradle\w*\s+test|make\s+test"
    r")(?=\s|$)"
)
# Test summaries that mean failure even when a pipe (`pytest | tail`) hides the exit code.
TEST_FAILED_OUTPUT = re.compile(
    r"\b\d+ (failed|errors?)\b|^FAILED |^ERROR |Tests:\s+\d+ failed|^--- FAIL|test result: FAILED", re.MULTILINE
)
EDIT_TOOLS = ("edit", "write")


@dataclass(frozen=True)
class Signal:
    name: str
    """test_fail_streak · same_file_edits · repeat_command · tool_error_streak · step_budget · cost_budget."""
    detail: str
    step: int


def is_test_command(command: str) -> bool:
    return bool(TEST_COMMAND.search(command))


def normalize(command: str) -> str:
    return " ".join(command.split())


def call_key(tool: str, args: dict[str, Any]) -> str:
    """What makes two tool calls "the same": the bash command, or the tool with its arguments."""
    if tool == "bash":
        return "bash:" + normalize(str(args.get("command", "")))
    return f"{tool}:" + str(sorted((k, str(v)) for k, v in args.items()))


@dataclass
class SignalMonitor:
    """Watches one attempt. Fed pi's tool events; returns a signal once one fires."""

    config: SignalConfig
    steps: int = 0
    """Assistant turns so far."""
    fail_streak: int = 0
    error_streak: int = 0
    edits: Counter = field(default_factory=Counter)
    last_call: str = ""
    repeat_count: int = 0

    def turn(self) -> None:
        self.steps += 1

    def tool_start(self, tool: str, args: dict[str, Any]) -> Signal | None:
        """A tool call is about to run."""
        step = self.steps
        if tool in EDIT_TOOLS and args.get("path"):
            path = str(args["path"])
            self.edits[path] += 1
            if self.edits[path] >= self.config.same_file_edits:
                return Signal("same_file_edits", f"edited {path} {self.edits[path]} times", step)
        current = call_key(tool, args)
        self.repeat_count = self.repeat_count + 1 if current == self.last_call else 1
        self.last_call = current
        if self.repeat_count >= self.config.repeat_command:
            return Signal("repeat_command", f"made the same call {self.repeat_count} times: {current[:120]}", step)
        return None

    def tool_end(self, tool: str, args: dict[str, Any], is_error: bool, output: str = "") -> Signal | None:
        """A tool call finished; `is_error` is pi's verdict (a bash command exiting non-zero counts)."""
        step = self.steps
        self.error_streak = self.error_streak + 1 if is_error else 0
        if tool == "bash" and is_test_command(str(args.get("command", ""))):
            failed = is_error or bool(TEST_FAILED_OUTPUT.search(output))
            self.fail_streak = self.fail_streak + 1 if failed else 0
            if self.fail_streak >= self.config.test_fail_streak:
                return Signal("test_fail_streak", f"{self.fail_streak} failing test runs in a row", step)
        if self.error_streak >= self.config.tool_error_streak:
            return Signal("tool_error_streak", f"{self.error_streak} tool calls in a row returned errors", step)
        return None
