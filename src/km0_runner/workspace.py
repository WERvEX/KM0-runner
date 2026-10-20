"""Workspaces: one git worktree and branch per task, so pi never touches the user's checkout.

A task without upstream tasks starts from the commit the run started on; a task with upstream tasks starts
from their branches, merged in order. When two upstream branches conflict, a resolver (pi, in practice) may fix
the conflicted files; the Runner then commits the merge and the task starts on top of it. The Runner commits
the task's changes to its branch and removes the worktree; it never pushes and never touches the user's
branches.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .handoff import HANDOFF_ROOT

AUTHOR = ["-c", "user.name=KM0 Runner", "-c", "user.email=km0-runner@localhost"]
# Never commit the package folder or bytecode the agent's test runs left behind.
EXCLUDES = [f":!{HANDOFF_ROOT}", ":!**/__pycache__/**", ":!*.pyc"]


class GitError(RuntimeError):
    pass


class MergeConflict(GitError):
    pass


Resolver = Callable[[Path, list[str], str], str | None]
"""Fixes a conflicted merge in the worktree at `path`: (path, conflicted files, branch being merged in).
Returns None when the conflict is resolved, otherwise why not."""

MARKER = re.compile(r"^(<<<<<<<|>>>>>>>)( |$)|^=======$", re.MULTILINE)


def git(cwd: str | Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {cwd}: {(result.stderr or result.stdout).strip()[-1000:]}")
    return result.stdout.strip()


@dataclass
class Workspace:
    task_id: str
    path: Path
    branch: str
    base_commit: str
    """Where the task started: the run's start commit, or its upstream branches merged."""
    resolved: list[str] = field(default_factory=list)
    """Files whose merge conflict between upstream branches was resolved before the task started."""


class Workspaces:
    def __init__(
        self,
        repo: str | Path,
        run_id: str,
        root: str | Path,
        package_root: Path | None = None,
        start_commit: str | None = None,
    ):
        self.repo = Path(repo).resolve()
        self.run_id = run_id
        self.root = Path(root).resolve()
        self.package_root = package_root
        if git(self.repo, "rev-parse", "--is-inside-work-tree", check=False) != "true":
            raise GitError(f"{self.repo} is not a git repository")
        self.start_commit = git(self.repo, "rev-parse", start_commit or "HEAD")
        self.reused: dict[str, str] = {}
        """Upstream tasks this run does not redo, by task id: the branch an earlier run left them on."""

    def branch(self, task_id: str) -> str:
        return self.reused.get(task_id) or f"km0/{self.run_id}/{task_id}"

    def reuse(self, run_id: str, task_ids: list[str]) -> list[str]:
        """Take these tasks from run `run_id`'s branches where they exist; returns the ids found."""
        found = []
        for task_id in task_ids:
            branch = f"km0/{run_id}/{task_id}"
            if git(self.repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False):
                self.reused[task_id] = branch
                found.append(task_id)
        return found

    def create(self, task_id: str, upstream: list[str], resolve: Resolver | None = None) -> Workspace:
        """A worktree on a new branch; raises `MergeConflict` when the upstream branches do not merge and
        `resolve` is missing or fails."""
        path = self.root / task_id
        branch = self.branch(task_id)
        start = self.branch(upstream[0]) if upstream else self.start_commit
        self.root.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), start)
        resolved: list[str] = []
        for other in upstream[1:]:
            merged = subprocess.run(
                ["git", *AUTHOR, "merge", "-q", "--no-edit", self.branch(other)], cwd=path, capture_output=True, text=True
            )
            if merged.returncode == 0:
                continue
            files = git(path, "diff", "--name-only", "--diff-filter=U", check=False).split()
            why = resolve(path, files, self.branch(other)) if resolve and files else "no resolver"
            if why is None:
                why = self._conclude_merge(path, files, self.branch(other))
            if why is not None:
                git(path, "merge", "--abort", check=False)
                self.discard(Workspace(task_id, path, branch, start))
                raise MergeConflict(
                    f"merging {self.branch(other)} into {branch} conflicts in {', '.join(files) or '?'}: {why}"
                )
            resolved += files
        workspace = Workspace(task_id, path, branch, git(path, "rev-parse", "HEAD"), resolved)
        self._copy_package(workspace)
        return workspace

    def _conclude_merge(self, path: Path, files: list[str], other: str) -> str | None:
        """Commit a merge the resolver fixed; why not, when it left the merge unfinished."""
        if not git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False):
            # The resolver may have committed the merge itself, against instructions; that still counts.
            if git(path, "rev-parse", "-q", "--verify", "HEAD^2", check=False) == git(path, "rev-parse", other):
                return None
            return "the merge was aborted during resolution"
        marked = [f for f in files if (path / f).is_file() and MARKER.search((path / f).read_text(errors="replace"))]
        if marked:
            return f"conflict markers left in {', '.join(marked)}"
        git(path, "add", "-A", "--", ".", *EXCLUDES)
        if git(path, "diff", "--name-only", "--diff-filter=U", check=False):
            return "files still unmerged"
        git(path, *AUTHOR, "commit", "-q", "--no-verify", "--no-edit")
        return None

    def merge_diff(self, workspace: Workspace) -> str:
        """How the conflicts were resolved: the merge commit against git's own conflicted merge."""
        return git(workspace.path, "show", "--remerge-diff", "--format=", workspace.base_commit, check=False)

    def _copy_package(self, workspace: Workspace) -> None:
        """Put the package's `km0/` folder in the worktree when the repository does not track it."""
        target = workspace.path / HANDOFF_ROOT
        if self.package_root is None or (target / "tasks.json").exists():
            return
        shutil.copytree(self.package_root, target, dirs_exist_ok=True)

    def reset(self, workspace: Workspace) -> None:
        """Back to where the task started, for a `restart` handoff."""
        git(workspace.path, "reset", "-q", "--hard", workspace.base_commit)
        git(workspace.path, "clean", "-fdq", "-e", f"{HANDOFF_ROOT}/")
        self._copy_package(workspace)

    def changed(self, workspace: Workspace) -> bool:
        return bool(git(workspace.path, "status", "--porcelain", "--", ".", *EXCLUDES))

    def commit(self, workspace: Workspace, message: str) -> str | None:
        """Commit everything the task changed, outside `km0/`; None when it changed nothing."""
        git(workspace.path, "add", "-A", "--", ".", *EXCLUDES)
        if not git(workspace.path, "diff", "--cached", "--name-only"):
            return None
        git(workspace.path, *AUTHOR, "commit", "-q", "--no-verify", "-m", message)
        return git(workspace.path, "rev-parse", "HEAD")

    def diff(self, workspace: Workspace, commit: str) -> str:
        return subprocess.run(
            ["git", "diff", workspace.base_commit, commit], cwd=workspace.path, capture_output=True, text=True
        ).stdout

    def working_diff(self, workspace: Workspace) -> str:
        """Files changed since the task's start, new ones included, for handoff notes."""
        return git(workspace.path, "status", "--porcelain", "--untracked-files=all", "--", ".", *EXCLUDES, check=False)

    def remove(self, workspace: Workspace) -> None:
        """Drop the worktree and keep the branch."""
        git(self.repo, "worktree", "remove", "--force", str(workspace.path), check=False)
        if workspace.path.exists():
            shutil.rmtree(workspace.path, ignore_errors=True)
        git(self.repo, "worktree", "prune", check=False)

    def discard(self, workspace: Workspace) -> None:
        """Drop the worktree and its branch: the task never started."""
        self.remove(workspace)
        git(self.repo, "branch", "-q", "-D", workspace.branch, check=False)
