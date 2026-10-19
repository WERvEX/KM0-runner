"""Reading tasks from a KM0 handoff package (and, optionally, SWE-bench instances) into `RunTask`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

HANDOFF_ROOT = "km0"


class RunTask(BaseModel):
    task_id: str
    title: str
    goal: str = ""
    prompt: str
    predicted_tier: str | None
    predicted_features: dict[str, Any] | None = None
    """KM0's `TaskFeatures` behind the tier; calibration replays the rules on them."""
    predicted_score: int | None = None
    depends_on: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    test_command: str | None = None
    retry: bool = False
    """KM0 hands the task off again after a failure or a rejection; it is never explored lower."""
    source: Literal["km0", "swebench"] = "km0"
    instance: dict[str, Any] | None = None
    """The SWE-bench instance, for the harness judge."""

    @property
    def risky(self) -> bool:
        return bool((self.predicted_features or {}).get("risks"))


class Package(BaseModel):
    root: Path
    """The folder holding `tasks.json`, copied into each workspace as `km0/` when the repository lacks it."""
    project_id: int | str | None = None
    title: str = ""
    tasks: list[RunTask]


def pad(seq: int) -> str:
    return f"{seq:02d}"


def find_manifest(path: Path) -> Path:
    for candidate in (path / HANDOFF_ROOT / "tasks.json", path / "tasks.json"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no tasks.json in {path} or {path / HANDOFF_ROOT}")


def load_km0_package(path: str | Path) -> Package:
    """A KM0 handoff: a `km0/` folder (or a repository holding one), or the package JSON KM0 builds.

    `tasks.json` lists the tasks; each `prompt` is a path such as `km0/tasks/01.md`, relative to the folder
    that holds `km0/`. An optional `manifest.json` beside it carries the project id.
    """
    path = Path(path).resolve()
    if path.is_file():
        return _from_package_json(path)
    manifest = find_manifest(path)
    root = manifest.parent
    base = root.parent
    meta = json.loads((root / "manifest.json").read_text(encoding="utf-8")) if (root / "manifest.json").is_file() else {}
    tasks = [
        _task(entry, (base / entry["prompt"]).read_text(encoding="utf-8"))
        for entry in json.loads(manifest.read_text(encoding="utf-8"))
    ]
    return Package(root=root, project_id=meta.get("projectId"), title=meta.get("title", ""), tasks=_sorted(tasks))


def _from_package_json(path: Path) -> Package:
    """KM0's `HandoffPackage` JSON: unpack its files next to it, then read them like a folder."""
    package = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent / f"{path.stem}-unpacked"
    for file in package["files"]:
        target = base / file["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file["content"], encoding="utf-8")
    loaded = load_km0_package(base)
    if loaded.project_id is None:
        loaded.project_id = package.get("projectId")
    return loaded


def _task(entry: dict[str, Any], prompt: str) -> RunTask:
    return RunTask(
        task_id=pad(entry["seq"]),
        title=entry.get("title", ""),
        goal=entry.get("goal", ""),
        prompt=prompt,
        predicted_tier=entry.get("tier"),
        predicted_features=entry.get("features"),
        predicted_score=entry.get("score"),
        depends_on=[pad(seq) for seq in entry.get("dependsOn", [])],
        acceptance=entry.get("acceptance", []),
        test_command=entry.get("testCommand") or None,
        retry=bool(entry.get("retry")),
    )


def _sorted(tasks: list[RunTask]) -> list[RunTask]:
    return sorted(tasks, key=lambda task: task.task_id)


def read_rows(path: str | Path) -> list[dict[str, Any]]:
    """Rows of a JSON array or JSONL file."""
    text = Path(path).read_text(encoding="utf-8").strip()
    return json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line]


def load_swebench_instances(
    path: str | Path, predicted: dict[str, Any] | None = None, ids: list[str] | None = None
) -> list[RunTask]:
    """SWE-bench instances, with KM0's predictions when given.

    `predicted` maps instance id to a tier (`"T2"`) or to `{"tier": "T2", "features": {...}}`.
    """
    wanted = set(ids) if ids else None
    tasks = []
    for row in read_rows(path):
        if wanted is not None and row["instance_id"] not in wanted:
            continue
        guess = (predicted or {}).get(row["instance_id"])
        guess = {"tier": guess} if isinstance(guess, str) else guess or {}
        tasks.append(
            RunTask(
                task_id=row["instance_id"],
                title=row["instance_id"],
                prompt=row["problem_statement"],
                predicted_tier=guess.get("tier"),
                predicted_features=guess.get("features"),
                source="swebench",
                instance=row,
            )
        )
    return tasks
