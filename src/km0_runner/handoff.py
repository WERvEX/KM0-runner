"""Reading tasks: KM0 handoff packages and SWE-bench instances, both turned into `RunTask`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from .tiers import Tier


class RunTask(BaseModel):
    task_id: str
    title: str
    prompt: str
    predicted_tier: Tier | None
    source: Literal["km0", "swebench"]
    instance: dict[str, Any] | None = None
    """The SWE-bench instance, for the environment and the judge."""


def pad(seq: int) -> str:
    return f"{seq:02d}"


def load_km0_package(path: str | Path) -> list[RunTask]:
    """Tasks from a KM0 handoff, either the package JSON KM0 builds or a repository it was unpacked into.

    The package JSON is KM0's `HandoffPackage` (`packages/shared/src/handoff.ts`). An unpacked repository
    has `km0/tasks.json` and one prompt file per task under `km0/tasks/`.
    """
    path = Path(path)
    if path.is_dir():
        manifest = path / "km0" / "tasks.json" if (path / "km0").is_dir() else path / "tasks.json"
        base = manifest.parent.parent
        tasks = json.loads(manifest.read_text(encoding="utf-8"))
        return [
            RunTask(
                task_id=pad(task["seq"]),
                title=task["title"],
                prompt=(base / task["prompt"]).read_text(encoding="utf-8"),
                predicted_tier=task["tier"],
                source="km0",
            )
            for task in sorted(tasks, key=lambda task: task["seq"])
        ]

    package = json.loads(path.read_text(encoding="utf-8"))
    return [
        RunTask(
            task_id=pad(task["seq"]),
            title=task["title"],
            prompt=task["prompt"],
            predicted_tier=task["tier"],
            source="km0",
        )
        for task in sorted(package["tasks"], key=lambda task: task["seq"])
    ]


def unpack_km0_package(path: str | Path, repo: str | Path) -> None:
    """Write a package JSON's files into a repository, as KM0's own export does."""
    package = json.loads(Path(path).read_text(encoding="utf-8"))
    for file in package["files"]:
        target = Path(repo) / file["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file["content"], encoding="utf-8")


def load_swebench_instances(
    path: str | Path, predicted: dict[str, Tier] | None = None, ids: list[str] | None = None
) -> list[RunTask]:
    """SWE-bench instances from a JSON array or JSONL file, with KM0's tiers from `predicted` when given."""
    text = Path(path).read_text(encoding="utf-8").strip()
    rows = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line]
    wanted = set(ids) if ids else None
    return [
        RunTask(
            task_id=row["instance_id"],
            title=row["instance_id"],
            prompt=row["problem_statement"],
            predicted_tier=(predicted or {}).get(row["instance_id"]),
            source="swebench",
            instance=row,
        )
        for row in rows
        if wanted is None or row["instance_id"] in wanted
    ]
