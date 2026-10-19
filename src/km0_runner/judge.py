"""The Runner's own check after each attempt: the task's test command, run in its workspace.

A pass is an escalation signal and evidence for KM0, not the final word: KM0 accepts or rejects the work.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .report import TestResult


# Bytecode written by an earlier run can shadow a same-size edit made within the same second.
TEST_ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def run_test_command(
    command: str | None, workspace: Path, timeout: int = 900, docker_image: str | None = None
) -> TestResult:
    if not command:
        return TestResult(passed=None, judge="none", detail="no test command; KM0 judges this task")
    argv = ["bash", "-lc", command]
    if docker_image:
        argv = ["docker", "run", "--rm", "--network", "host", "-v", f"{workspace}:/workspace", "-w", "/workspace", docker_image, *argv]
    try:
        result = subprocess.run(argv, cwd=workspace, env=TEST_ENV, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TestResult(passed=False, judge="command", detail=f"`{command}` timed out after {timeout}s")
    tail = (result.stdout + result.stderr)[-3000:]
    return TestResult(passed=result.returncode == 0, judge="command", detail=f"`{command}` exited {result.returncode}\n{tail}")
