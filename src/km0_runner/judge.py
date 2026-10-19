"""Judges decide whether an attempt solved its task. Results count only by tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Protocol

from minisweagent import Environment

from .handoff import RunTask
from .report import TestResult


class Judge(Protocol):
    name: str

    def judge(self, task: RunTask, env: Environment, patch: str) -> TestResult: ...


class NoJudge:
    """For dry runs: every submission counts as unjudged, so nothing escalates on test results."""

    name = "none"

    def judge(self, task: RunTask, env: Environment, patch: str) -> TestResult:
        return TestResult(passed=None, judge=self.name, detail="no judge configured")


class CommandJudge:
    """Runs a test command in the task's environment; exit code 0 means solved. For KM0 tasks."""

    name = "command"

    def __init__(self, command: str, timeout: int = 900):
        self.command = command
        self.timeout = timeout

    def judge(self, task: RunTask, env: Environment, patch: str) -> TestResult:
        try:
            output = env.execute({"command": self.command}, timeout=self.timeout)
        except Exception as error:  # Submitted and friends: a judge command must not end the run
            return TestResult(passed=False, judge=self.name, detail=f"judge command raised {error!r}")
        tail = output.get("output", "")[-2000:]
        return TestResult(
            passed=output.get("returncode") == 0,
            judge=self.name,
            detail=f"`{self.command}` exited {output.get('returncode')}\n{tail}",
        )


class SwebenchHarnessJudge:
    """The official SWE-bench harness, run on one attempt's patch in a fresh container."""

    name = "swebench"

    def __init__(self, dataset: str, workdir: str | Path, timeout: int = 1800):
        self.dataset = dataset
        self.workdir = Path(workdir)
        self.timeout = timeout

    def judge(self, task: RunTask, env: Environment, patch: str) -> TestResult:
        if not patch.strip():
            return TestResult(passed=False, judge=self.name, detail="empty patch")
        run_id = f"km0-runner-{uuid.uuid4().hex[:8]}"
        model = "km0-runner"
        self.workdir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".json", dir=self.workdir, delete=False) as file:
            json.dump([{"instance_id": task.task_id, "model_name_or_path": model, "model_patch": patch}], file)
            predictions = file.name
        command = [
            sys.executable, "-m", "swebench.harness.run_evaluation",
            "--dataset_name", self.dataset,
            "--predictions_path", predictions,
            "--instance_ids", task.task_id,
            "--run_id", run_id,
            "--max_workers", "1",
            "--timeout", str(self.timeout),
        ]  # fmt: skip
        result = subprocess.run(command, cwd=self.workdir, capture_output=True, text=True)
        report = self.workdir / "logs" / "run_evaluation" / run_id / model / task.task_id / "report.json"
        if not report.exists():
            tail = (result.stdout + result.stderr)[-2000:]
            return TestResult(passed=False, judge=self.name, detail=f"harness wrote no report\n{tail}")
        verdict = json.loads(report.read_text())[task.task_id]
        return TestResult(passed=bool(verdict.get("resolved")), judge=self.name, detail=f"report: {report}")
