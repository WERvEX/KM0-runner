"""Task order, skipping and exploration: which task runs when, and where it starts on the ladder."""

from __future__ import annotations

import hashlib
from graphlib import CycleError, TopologicalSorter

from .handoff import RunTask
from .tiers import RunnerConfig, Tier


class DependencyError(ValueError):
    pass


def order(tasks: list[RunTask]) -> list[RunTask]:
    """Tasks with every upstream task before them; ties keep the package's order. Fails on cycles and unknown ids."""
    by_id = {task.task_id: task for task in tasks}
    sorter: TopologicalSorter[str] = TopologicalSorter()
    for task in tasks:
        missing = [dep for dep in task.depends_on if dep not in by_id]
        if missing:
            raise DependencyError(f"task {task.task_id} depends on unknown tasks {missing}")
        sorter.add(task.task_id, *task.depends_on)
    try:
        sorter.prepare()
    except CycleError as error:
        raise DependencyError(f"dependency cycle: {' → '.join(error.args[1])}") from error
    position = {task.task_id: index for index, task in enumerate(tasks)}
    result: list[RunTask] = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready(), key=position.__getitem__)
        for task_id in ready:
            result.append(by_id[task_id])
            sorter.done(task_id)
    return result


def explore_draw(seed: int, run_id: str, task_id: str) -> float:
    """A reproducible number in [0, 1) for one task of one run."""
    digest = hashlib.sha256(f"{seed}:{run_id}:{task_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def start_tier(config: RunnerConfig, task: RunTask, run_id: str, forced: Tier | None = None) -> tuple[Tier, bool]:
    """The rung to start on and whether it is an exploration one tier below KM0's prediction."""
    if config.owns_ladder(task.task_id):
        # KM0 chose where the task starts when it wrote the task's own ladder; a forced rung must be on it.
        names = config.for_task(task.task_id).names
        return (forced if forced in names else names[0]), False
    if forced:
        return forced, False
    predicted = config.start_rung(task.predicted_tier)
    lower = config.below(predicted)
    exploration = config.exploration
    # A task KM0 hands off again after a failure or a rejection already showed a lower tier is not enough.
    if lower is None or exploration.rate <= 0 or task.retry or (exploration.only_low_risk and task.risky):
        return predicted, False
    if explore_draw(exploration.seed, run_id, task.task_id) < exploration.rate:
        return lower, True
    return predicted, False
