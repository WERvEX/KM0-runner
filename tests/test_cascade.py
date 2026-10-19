import json

from conftest import SUBMIT, ladder, scripted, step

from km0_runner.cascade import Cascade
from km0_runner.handoff import RunTask
from km0_runner.judge import CommandJudge

FIX = "sed -i 's/a - b/a + b/' calc.py"
CHECK = "python -c 'import calc; assert calc.add(2, 3) == 5'"


def task(tier="T1"):
    return RunTask(task_id="01", title="fix add", prompt="Make add() add.", predicted_tier=tier, source="km0")


def cascade(tmp_path, repo, scripts, **config):
    cfg = ladder(environment={"cwd": str(repo), "environment_class": "local"}, **config)
    return Cascade(cfg, tmp_path / "out", judge=CommandJudge(CHECK), model_factory=scripted(scripts))


def test_signal_escalates_and_higher_tier_solves(tmp_path, repo):
    scripts = {
        "cheap": [step("cat calc.py")] * 3,
        "mid": [step(FIX), step(SUBMIT)],
    }
    report = cascade(tmp_path, repo, scripts).run(task())

    assert report.predicted_tier == "T1"
    assert report.solved_tier == "T2"
    assert [(e.from_tier, e.to_tier, e.signal) for e in report.escalations] == [("T1", "T2", "repeat_command")]
    assert [a.tier for a in report.attempts] == ["T1", "T2"]
    assert report.cost == 0.05 and report.steps == 5
    assert report.test_result.passed is True
    assert "a + b" in report.patch
    # The default handoff drops the cheap trajectory and passes a short note.
    traj = json.loads((tmp_path / "out" / report.attempts[1].trajectory).read_text())
    prompt = traj["messages"][1]["content"]
    assert "<handoff>" in prompt and "repeat_command" in prompt and "cat calc.py" not in prompt
    lines = (tmp_path / "out" / "01" / "steps.jsonl").read_text().splitlines()
    assert json.loads(lines[2])["signal"] == "repeat_command"


def test_starts_on_predicted_tier(tmp_path, repo):
    report = cascade(tmp_path, repo, {"mid": [step(FIX), step(SUBMIT)]}).run(task("T2"))
    assert report.start_tier == "T2" and report.solved_tier == "T2" and not report.escalations


def test_failed_submit_escalates_then_fails_on_top_tier(tmp_path, repo):
    scripts = {"strong": [step(SUBMIT)]}
    report = cascade(tmp_path, repo, scripts).run(task("T3"))
    assert report.solved_tier is None
    assert report.failure_category == "tests_failed"
    assert report.test_result.passed is False


def test_step_budget(tmp_path, repo):
    scripts = {"cheap": [step(f"echo {i}") for i in range(10)], "mid": [step(FIX), step(SUBMIT)]}
    cfg = {"tiers": {"T1": {"model_name": "cheap", "step_limit": 2}, "T2": {"model_name": "mid"}}}
    report = cascade(tmp_path, repo, scripts, **cfg).run(task())
    assert report.escalations[0].signal == "step_budget"
    assert report.attempts[0].steps == 2
    assert report.solved_tier == "T2"


def test_restart_handoff_resets_repository(tmp_path, repo):
    scripts = {
        "cheap": [step(f"echo broken{i} > calc.py") for i in range(5)],
        "mid": [step("cat calc.py"), step(FIX), step(SUBMIT)],
    }
    report = cascade(tmp_path, repo, scripts, handoff="restart").run(task())
    assert report.escalations[0].signal == "same_file_edits"
    assert report.solved_tier == "T2"
    traj = json.loads((tmp_path / "out" / report.attempts[1].trajectory).read_text())
    assert "<handoff>" not in traj["messages"][1]["content"]
    assert "a - b" in json.dumps(traj["messages"][3])  # the reset brought the original file back


def test_full_handoff_passes_transcript(tmp_path, repo):
    scripts = {"cheap": [step("cat calc.py")] * 3, "mid": [step(FIX), step(SUBMIT)]}
    report = cascade(tmp_path, repo, scripts, handoff="full").run(task())
    traj = json.loads((tmp_path / "out" / report.attempts[1].trajectory).read_text())
    assert "<transcript>" in traj["messages"][1]["content"] and "a - b" in traj["messages"][1]["content"]


def test_single_tier_ladder_runs_everything_there(tmp_path, repo):
    cfg = {"tiers": {"T3": {"model_name": "strong"}}}
    report = cascade(tmp_path, repo, {"strong": [step(FIX), step(SUBMIT)]}, **cfg).run(task("T1"))
    assert report.start_tier == "T3" and report.solved_tier == "T3"


def test_crash_is_reported_not_raised(tmp_path, repo):
    report = cascade(tmp_path, repo, {}).run(task())
    assert report.failure_category == "error"
    assert (tmp_path / "out" / "01" / "report.json").exists()


def test_failed_submit_can_stop_instead_of_escalating(tmp_path, repo):
    report = cascade(tmp_path, repo, {"cheap": [step(SUBMIT)]}, signals={"escalate_on_failed_submit": False}).run(task())
    assert report.failure_category == "tests_failed" and not report.escalations
