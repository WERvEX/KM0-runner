"""A task's own ladder from the config's `task_ladders`, which KM0 writes from each task's escalation chain."""

import json

import pytest
from conftest import bash, done, edit, ladder
from pydantic import ValidationError
from test_cascade import CHECK, FIX, run, write_package

from km0_runner.handoff import RunTask
from km0_runner.scheduler import start_tier
from km0_runner.tiers import RunnerConfig

OWN = {"1": [{"name": "T2", "model": "fake/other"}, {"name": "T2-2", "model": "fake/strong", "step_limit": 4}]}


def test_a_task_climbs_its_own_ladder_and_the_others_keep_the_runs(tmp_path, repo):
    write_package(repo, [{"tier": "T1"}, {"tier": "T1"}], commit=True)
    runs = {"other": [[bash("echo wrong >> notes.txt"), done()]], "strong": [[FIX, done()]], "cheap": [[FIX, done()]]}
    fake, run_dir, [one, two] = run(tmp_path, repo, runs, shared=True, task_ladders=OWN, exploration={"rate": 1.0})

    # Task 1 starts on its ladder's first rung, not on KM0's tier, and is never explored lower.
    assert one.start_tier == "T2" and not one.explored and one.solved_tier == "T2-2"
    assert [(a.tier, a.model) for a in one.attempts] == [("T2", "fake/other"), ("T2-2", "fake/strong")]
    assert [(e.from_tier, e.to_tier) for e in one.escalations] == [("T2", "T2-2")]
    # Limits left out come from the run's rung of the same name; others keep what the task's ladder says.
    assert [(r.name, r.model, r.step_limit) for r in one.ladder] == [("T2", "fake/other", 10), ("T2-2", "fake/strong", 4)]
    # Task 2 has no ladder of its own: the run's ladder, on KM0's tier.
    assert two.ladder is None and two.start_tier == "T1" and [a.model for a in two.attempts] == ["fake/cheap"]
    assert [model for model, _ in fake.prompts()] == ["other", "strong", "cheap"]

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["tiers"]["T2-2"] == {"attempts": 1, "solved": 1, "cost": pytest.approx(0.4)}  # two replies at $0.2


def test_conflicts_before_the_task_are_resolved_on_its_own_first_rung(tmp_path, repo):
    write_package(repo, [{}, {}, {"dependsOn": [1, 2]}], commit=True)
    resolve = bash("printf 'def add(a, b):\\n    return a + b\\n' > calc.py")
    runs = {"cheap": [[FIX, done()], [edit("calc.py", "a - b", "b + a"), done()]], "other": [[resolve, done()], [bash(CHECK), done()]]}
    _, _, [_, _, three] = run(tmp_path, repo, runs, shared=True, task_ladders={"03": OWN["1"]})

    assert three.merge_resolved == ["calc.py"] and three.solved_tier == "T2"
    assert [(a.purpose, a.model) for a in three.attempts] == [("merge", "fake/other"), ("task", "fake/other")]


def test_a_forced_start_must_be_on_the_tasks_ladder():
    config = ladder(task_ladders=OWN)
    task = RunTask(task_id="01", title="t", prompt="p", predicted_tier="T1")
    assert start_tier(config, task, "r1", forced="T2-2") == ("T2-2", False)
    assert start_tier(config, task, "r1", forced="T3") == ("T2", False)  # T3 is the run's, not the task's


@pytest.mark.parametrize(
    "rungs, problem",
    [
        ([], "at least one rung"),
        ([{"name": "T2", "model": "fake/a"}, {"name": "T2", "model": "fake/b"}], "unique names"),
        ([{"name": "T2", "model": "no-provider"}], "provider/modelId"),
    ],
)
def test_a_bad_task_ladder_is_refused_before_anything_runs(rungs, problem):
    with pytest.raises(ValidationError, match=problem):
        RunnerConfig.model_validate({"ladder": [{"name": "T1", "model": "fake/cheap"}], "task_ladders": {"01": rungs}})
