"""Optional SWE-bench regression benchmark: run instances through the same cascade, judged by the harness.

Needs the `swebench` extra and Docker for the harness. Each instance's repository is cloned once into a
cache and checked out at its base commit in a worktree; the harness grades the attempt's diff.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from .cascade import Cascade, PiFactory
from .handoff import RunTask
from .report import ExecutionReport, TestResult
from .scheduler import start_tier
from .tiers import RunnerConfig, Tier
from .workspace import EXCLUDES, Workspace, Workspaces, git

DATASETS = {
    "verified": "princeton-nlp/SWE-bench_Verified",
    "lite": "princeton-nlp/SWE-bench_Lite",
}


def clone(repo: str, cache: Path) -> Path:
    target = cache / repo.replace("/", "__")
    if not (target / ".git").exists():
        cache.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", f"https://github.com/{repo}.git", str(target)], check=True)
    return target


class HarnessJudge:
    """The official harness on the workspace's diff, in a fresh container."""

    def __init__(self, dataset: str, workdir: Path, timeout: int = 1800):
        self.dataset = DATASETS.get(dataset, dataset)
        self.workdir = workdir
        self.timeout = timeout

    def __call__(self, task: RunTask, workspace: Workspace) -> TestResult:
        git(workspace.path, "add", "-A", "--", ".", *EXCLUDES)
        patch = git(workspace.path, "diff", "--cached", workspace.base_commit)
        git(workspace.path, "reset", "-q")
        if not patch.strip():
            return TestResult(passed=False, judge="swebench", detail="empty patch")
        run_id = f"km0-runner-{uuid.uuid4().hex[:8]}"
        model = "km0-runner"
        self.workdir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".json", dir=self.workdir, delete=False) as file:
            json.dump([{"instance_id": task.task_id, "model_name_or_path": model, "model_patch": patch + "\n"}], file)
        command = [
            sys.executable, "-m", "swebench.harness.run_evaluation", "--dataset_name", self.dataset,
            "--predictions_path", file.name, "--instance_ids", task.task_id, "--run_id", run_id,
            "--max_workers", "1", "--timeout", str(self.timeout),
        ]  # fmt: skip
        result = subprocess.run(command, cwd=self.workdir, capture_output=True, text=True)
        report = self.workdir / "logs" / "run_evaluation" / run_id / model / task.task_id / "report.json"
        if not report.exists():
            return TestResult(passed=False, judge="swebench", detail=f"harness wrote no report\n{(result.stdout + result.stderr)[-2000:]}")
        verdict = json.loads(report.read_text())[task.task_id]
        return TestResult(passed=bool(verdict.get("resolved")), judge="swebench", detail=f"report: {report}")


def run_instances(
    tasks: list[RunTask],
    config: RunnerConfig,
    run_dir: Path,
    run_id: str,
    pi_factory: PiFactory,
    pi_version: str | None,
    dataset: str = "verified",
    judge: bool = True,
    repeats: int = 1,
    start: Tier | None = None,
) -> list[ExecutionReport]:
    cache = run_dir.parent / "_repos"
    check = HarnessJudge(dataset, run_dir / "_harness") if judge else (lambda task, ws: TestResult(passed=None, judge="none"))
    reports = []
    (run_dir / "reports").mkdir(parents=True, exist_ok=True)
    for task in tasks:
        assert task.instance is not None
        repo = clone(task.instance["repo"], cache)
        workspaces = Workspaces(repo, run_id, run_dir / "worktrees", start_commit=task.instance["base_commit"])
        cascade = Cascade(config, run_dir, run_id, workspaces, pi_factory, pi_version, check=check)
        for run_index in range(repeats):
            key = task.task_id if run_index == 0 else f"{task.task_id}.r{run_index}"
            workspace = workspaces.create(key, [])
            tier, explored = start_tier(config, task, run_id, start)
            try:
                report = cascade.run(task, workspace, tier, explored, run_index=run_index)
            finally:
                workspaces.remove(workspace)
            (run_dir / "reports" / f"{key}.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
            reports.append(report)
            print(f"{key}: start={report.start_tier} solved={report.solved_tier} cost=${report.cost:.4f}")
    return reports
