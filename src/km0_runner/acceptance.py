"""KM0's verdicts on a run (`runs/<run_id>/acceptance.jsonl`): the labels calibration learns from."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class Verdict(BaseModel):
    run_id: str | None = None
    task_id: str
    accepted: bool
    reason: str = ""
    judged_by: str = ""
    """The model KM0 judged with."""
    user_override: bool | None = None
    """The user's verdict after a spot check; wins over `accepted`."""
    judged_at: str = ""

    @property
    def final(self) -> bool:
        return self.accepted if self.user_override is None else self.user_override


def load_verdicts(paths: list[str | Path]) -> dict[tuple[str, str], Verdict]:
    """(run_id, task_id) → verdict, from run directories or acceptance files; a later line wins."""
    verdicts: dict[tuple[str, str], Verdict] = {}
    for path in map(Path, paths):
        files = sorted(path.glob("**/acceptance.jsonl")) if path.is_dir() else [path]
        for file in files:
            run_id = file.parent.name
            for line in file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    verdict = Verdict.model_validate_json(line)
                    verdicts[(verdict.run_id or run_id, verdict.task_id)] = verdict
    return verdicts
