import json
import subprocess

import pytest
from conftest import FakePi, bash, done, edit, ladder

from km0_runner.handoff import RunTask, load_km0_package
from km0_runner.runner import run_package
from km0_runner.scheduler import DependencyError, order, start_tier

CHECK = "python -c 'import calc; assert calc.add(2, 3) == 5'"
FIX = edit("calc.py", "a - b", "a + b")


def write_package(repo, tasks, commit=False):
    """A km0/ folder as KM0 exports it: tasks.json plus one prompt per task."""
    root = repo / "km0"
    (root / "tasks").mkdir(parents=True)
    entries = []
    for seq, extra in enumerate(tasks, start=1):
        (root / "tasks" / f"{seq:02d}.md").write_text(f"Task {seq}: make add() add.")
        entries.append({"seq": seq, "title": f"task {seq}", "goal": "add adds", "acceptance": ["WHEN add(2, 3) THEN 5"],
                        "files": ["calc.py"], "dependsOn": [], "tier": "T1", "prompt": f"km0/tasks/{seq:02d}.md",
                        "testCommand": CHECK, **extra})
    (root / "tasks.json").write_text(json.dumps(entries))
    (root / "manifest.json").write_text(json.dumps({"projectId": 7, "title": "calc"}))
    if commit:
        subprocess.run("git add -A && git -c user.email=t@t -c user.name=t commit -qm pkg", shell=True, cwd=repo, check=True)
    return root


def run(tmp_path, repo, runs, package=None, shared=False, **config):
    fake = FakePi(tmp_path, runs, price={"cheap": 0.01, "mid": 0.05, "strong": 0.2}, shared=shared)
    package = load_km0_package(package or repo)
    run_dir, reports = run_package(package, repo, ladder(**config), tmp_path / "runs", "r1", pi_factory=fake,
                                   pi_version="1.3.0", log=lambda line: None)
    return fake, run_dir, reports


def branch_file(repo, branch, path="calc.py"):
    return subprocess.run(["git", "show", f"{branch}:{path}"], cwd=repo, capture_output=True, text=True).stdout


def test_cheap_tier_solves_and_commits_to_its_branch(tmp_path, repo):
    write_package(repo, [{}], commit=True)
    fake, run_dir, [report] = run(tmp_path, repo, {"cheap": [[FIX, bash(CHECK), done()]]})

    assert report.report_version == 2 and report.project_id == 7 and report.run_id == "r1"
    assert report.start_tier == "T1" and report.solved_tier == "T1" and not report.escalations
    assert report.test_result.passed and report.failure_category is None
    assert report.branch == "km0/r1/01" and report.commit
    assert "a + b" in branch_file(repo, "km0/r1/01")
    assert "a - b" in (repo / "calc.py").read_text()  # the user's checkout is untouched
    assert report.acceptance == ["WHEN add(2, 3) THEN 5"]
    assert report.cost == pytest.approx(0.03) and report.steps == 3 and report.tokens.input == 300
    diff = (run_dir / report.diff).read_text(encoding="utf-8")
    assert "+    return a + b" in diff and "km0/" not in diff
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["passed"] == 1 and summary["tiers"]["T1"]["attempts"] == 1
    assert not (run_dir / "worktrees" / "01").exists()
    # The Runner's note tells pi how to finish.
    model, prompt = fake.prompts()[0]
    assert model == "cheap" and prompt.startswith("Task 1") and f"run `{CHECK}`" in prompt and "Do not commit" in prompt


def test_a_file_that_is_not_utf8_still_gets_a_diff(tmp_path, repo):
    write_package(repo, [{}], commit=True)
    gbk = bash("python -c \"open('notes.txt', 'wb').write('试用期'.encode('gbk'))\"")
    _, run_dir, [report] = run(tmp_path, repo, {"cheap": [[FIX, gbk, done()]]})

    assert report.solved_tier == "T1" and report.commit
    assert "notes.txt" in (run_dir / report.diff).read_text(encoding="utf-8")


def test_failing_tests_escalate_with_a_discard_handoff(tmp_path, repo):
    write_package(repo, [{}], commit=True)
    runs = {"cheap": [[bash("echo wrong >> notes.txt"), done()]], "mid": [[FIX, done()]]}
    fake, _, [report] = run(tmp_path, repo, runs)

    assert report.solved_tier == "T2"
    assert [(e.from_tier, e.to_tier, e.signal) for e in report.escalations] == [("T1", "T2", "tests_failed")]
    assert [a.tier for a in report.attempts] == ["T1", "T2"]
    assert [a.cost for a in report.attempts] == [pytest.approx(0.02), pytest.approx(0.1)]
    kinds = [c["type"] for c in fake.commands()]
    assert kinds.index("new_session") < kinds.index("set_model") < len(kinds) - 1
    model, second = fake.prompts()[1]
    assert model == "mid" and second.startswith("Task 1") and "git diff" in second and "notes.txt" in second
    assert "wrong" in branch_file(repo, "km0/r1/01", "notes.txt")  # the cheap tier's work is kept


def test_trajectory_signal_aborts_the_run(tmp_path, repo):
    write_package(repo, [{}], commit=True)
    loop = [bash("cat calc.py")] * 5
    fake, _, [report] = run(tmp_path, repo, {"cheap": [loop], "mid": [[FIX, done()]]})

    assert report.escalations[0].signal == "repeat_command"
    assert report.attempts[0].stop_reason == "signal" and report.attempts[0].steps < 5
    assert any(c["type"] == "abort" for c in fake.commands())
    assert report.solved_tier == "T2"


def test_continue_handoff_keeps_the_session(tmp_path, repo):
    write_package(repo, [{}], commit=True)
    runs = {"cheap": [[done("gave up")]], "mid": [[FIX, done()]]}
    fake, _, [report] = run(tmp_path, repo, runs, handoff="continue")

    assert report.solved_tier == "T2"
    assert not any(c["type"] == "new_session" for c in fake.commands())
    assert fake.prompts()[1][1].startswith("A previous attempt")


def test_restart_handoff_resets_the_workspace(tmp_path, repo):
    write_package(repo, [{}], commit=True)
    runs = {"cheap": [[bash("echo junk > junk.txt"), done()]], "mid": [[bash("test ! -e junk.txt"), FIX, done()]]}
    _, _, [report] = run(tmp_path, repo, runs, handoff="restart")

    assert report.solved_tier == "T2"
    assert report.attempts[1].steps == 3
    assert branch_file(repo, "km0/r1/01", "junk.txt") == ""


def test_step_budget_and_top_tier_failure(tmp_path, repo):
    write_package(repo, [{}], commit=True)
    config = ladder()
    for rung in config.ladder:
        rung.step_limit = 2
    wander = [bash("ls"), bash("pwd"), bash("ls -a"), done()]
    fake = FakePi(tmp_path, {"cheap": [wander], "mid": [wander], "strong": [wander]})
    _, [report] = run_package(load_km0_package(repo), repo, config, tmp_path / "runs", "r1", pi_factory=fake,
                              pi_version="1.3.0", log=lambda line: None)

    assert [e.signal for e in report.escalations] == ["step_budget", "step_budget"]
    assert report.failure_category == "budget_exhausted" and report.solved_tier is None
    assert report.test_result.passed is False


def test_no_test_command_leaves_the_verdict_to_km0(tmp_path, repo):
    write_package(repo, [{"testCommand": None}], commit=True)
    _, _, [report] = run(tmp_path, repo, {"cheap": [[FIX, done()]]})

    assert report.test_result.passed is None and report.solved_tier is None and report.failure_category is None
    assert report.commit


def test_dependencies_chain_branches_and_block_on_failure(tmp_path, repo):
    write_package(repo, [{}, {"dependsOn": [1], "testCommand": "test -e two.txt"}, {"dependsOn": [3]}], commit=False)
    # Task 3 depends on itself: rejected before anything runs.
    with pytest.raises(DependencyError):
        run(tmp_path, repo, {})
    (repo / "km0").rename(tmp_path / "km0-old")
    write_package(repo, [{}, {"dependsOn": [1], "testCommand": "test -e two.txt"}, {"dependsOn": [2]}])
    # Task 2 never creates two.txt, so it fails on every tier and task 3 is blocked.
    runs = {"cheap": [[FIX, done()], [done()]], "mid": [[done()]], "strong": [[done()]]}
    _, run_dir, reports = run(tmp_path, repo, runs)
    one, two, three = reports

    assert one.solved_tier == "T1"
    assert two.base_commit == one.commit  # started from task 1's branch
    assert two.failure_category == "tests_failed"
    assert three.failure_category == "blocked" and three.start_tier is None and "02" in three.failure_detail
    # The package was not tracked: the Runner copied it into each worktree and kept it out of the commits.
    assert branch_file(repo, "km0/r1/01", "km0/tasks.json") == ""
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["blocked"] == 1 and summary["failed"] == 1


def test_order_and_exploration():
    tasks = [RunTask(task_id="02", title="", prompt="", predicted_tier="T2", depends_on=["01"]),
             RunTask(task_id="01", title="", prompt="", predicted_tier="T2")]
    assert [t.task_id for t in order(tasks)] == ["01", "02"]
    with pytest.raises(DependencyError):
        order([RunTask(task_id="01", title="", prompt="", predicted_tier="T1", depends_on=["09"])])

    config = ladder(exploration={"rate": 0.5, "seed": 3})
    risky = RunTask(task_id="03", title="", prompt="", predicted_tier="T2", predicted_features={"risks": ["security"]})
    draws = [start_tier(config, RunTask(task_id=f"{i:02d}", title="", prompt="", predicted_tier="T2"), "r")
             for i in range(200)]
    explored = [tier for tier, flag in draws if flag]
    assert set(explored) == {"T1"} and 70 < len(explored) < 130
    assert draws == [start_tier(config, RunTask(task_id=f"{i:02d}", title="", prompt="", predicted_tier="T2"), "r")
                     for i in range(200)]  # reproducible
    assert all(start_tier(config, risky, f"r{i}") == ("T2", False) for i in range(50))
    assert start_tier(config, RunTask(task_id="x", title="", prompt="", predicted_tier="T1"), "r") == ("T1", False)
    # KM0 hands this one off again after a rejection: it starts on its tier every time.
    lifted = RunTask(task_id="04", title="", prompt="", predicted_tier="T2", retry=True)
    assert all(start_tier(ladder(exploration={"rate": 1.0}), lifted, f"r{i}") == ("T2", False) for i in range(20))


def test_ladder_mapping():
    config = ladder(km0_tiers={"T1": "T2"})
    assert config.start_rung("T1") == "T2" and config.start_rung("T3") == "T3" and config.start_rung(None) == "T1"
    four = ladder(ladder=[{"name": n, "model": f"p/{n}"} for n in ("A", "B", "C", "D")])
    assert four.start_rung("T2") == "B" and four.above("C") == "D" and four.below("A") is None


def test_redo_one_task_on_top_of_an_earlier_run(tmp_path, repo):
    write_package(repo, [{}, {"dependsOn": [1], "testCommand": "test -e two.txt"}])
    runs = {"cheap": [[FIX, done()], [bash("touch two.txt"), done()]]}
    _, _, [one, _] = run(tmp_path, repo, runs)
    # KM0 rejected task 2; only it runs again, starting from task 1's accepted branch.
    (tmp_path / "again").mkdir()
    fake = FakePi(tmp_path / "again", {"cheap": [[bash("echo 2 > two.txt"), done()]]})
    _, [two] = run_package(load_km0_package(repo), repo, ladder(), tmp_path / "runs", "r2", pi_factory=fake,
                           pi_version="1.3.0", only=["02"], reuse="r1", log=lambda line: None)

    assert two.base_commit == one.commit and two.solved_tier == "T1"
    assert "a + b" in branch_file(repo, "km0/r2/02") and branch_file(repo, "km0/r2/02", "two.txt") == "2\n"
    # Without a run to reuse, the missing upstream task blocks it.
    _, [blocked] = run_package(load_km0_package(repo), repo, ladder(), tmp_path / "runs", "r3", pi_factory=fake,
                               pi_version="1.3.0", only=["02"], log=lambda line: None)
    assert blocked.failure_category == "blocked"


def test_a_request_pi_retried_is_not_an_error(tmp_path, repo):
    write_package(repo, [{}], commit=True)
    runs = {"cheap": [[{"provider_error": "Request timed out.", "retried": True}, FIX, done()]]}
    _, _, [report] = run(tmp_path, repo, runs)

    assert report.solved_tier == "T1" and report.failure_category is None


def test_a_request_pi_gave_up_on_fails_the_task(tmp_path, repo):
    write_package(repo, [{}], commit=True)
    runs = {"cheap": [[{"provider_error": "Request timed out."}]] * 3}
    fake, _, [report] = run(tmp_path, repo, runs, pi={"command": ["pi"], "provider_retries": 2, "provider_retry_delay": 0})

    assert report.solved_tier is None and report.failure_category == "error"
    assert "timed out" in report.failure_detail
    assert len(fake.prompts()) == 3  # the first prompt and two resumes


def test_a_provider_error_resumes_the_session_on_the_same_tier(tmp_path, repo):
    write_package(repo, [{}], commit=True)
    runs = {"cheap": [[{"provider_error": "Request timed out."}], [FIX, done()]]}
    fake, _, [report] = run(tmp_path, repo, runs, pi={"command": ["pi"], "provider_retry_delay": 0})

    assert report.solved_tier == "T1" and report.failure_category is None and not report.escalations
    assert [a.stop_reason for a in report.attempts] == ["error", "settled"]
    kinds = [c["type"] for c in fake.commands()]
    assert "new_session" not in kinds and "set_model" not in kinds  # same session, same model
    assert fake.prompts()[1][1].startswith("The connection to the model failed")


def test_conflicting_upstream_branches_are_resolved_before_the_task(tmp_path, repo):
    # Tasks 1 and 2 run in parallel and rewrite the same line; task 3 needs both.
    write_package(repo, [{}, {}, {"dependsOn": [1, 2]}], commit=True)
    resolve = bash("printf 'def add(a, b):\\n    return a + b\\n' > calc.py")
    runs = {"cheap": [[FIX, done()], [edit("calc.py", "a - b", "b + a"), done()], [resolve, done()],
                      [bash("touch three.txt"), done()]]}
    fake, run_dir, [_, two, three] = run(tmp_path, repo, runs, shared=True)

    assert three.solved_tier == "T1" and three.merge_resolved == ["calc.py"]
    merge, task = three.attempts
    assert merge.purpose == "merge" and merge.test_result.passed and task.purpose == "task"
    assert three.cost == pytest.approx(merge.cost + task.cost) and merge.cost > 0
    # The task's own diff leaves the resolution out; the merge diff shows it.
    assert "three.txt" in (run_dir / three.diff).read_text(encoding="utf-8") and "calc.py" not in (run_dir / three.diff).read_text(encoding="utf-8")
    assert "return a + b" in (run_dir / three.merge_diff).read_text(encoding="utf-8")
    parents = subprocess.run(["git", "rev-list", "--parents", "-n1", three.base_commit], cwd=repo,
                             capture_output=True, text=True, check=True).stdout.split()
    assert len(parents) == 3 and two.commit in parents
    assert "git merge km0/r1/02" in fake.prompts()[2][1]


def test_an_unresolved_conflict_blocks_the_task(tmp_path, repo):
    write_package(repo, [{}, {}, {"dependsOn": [1, 2]}], commit=True)
    runs = {"cheap": [[FIX, done()], [edit("calc.py", "a - b", "b + a"), done()], [done("gave up")]]}
    _, _, [_, _, three] = run(tmp_path, repo, runs, shared=True)

    assert three.failure_category == "blocked" and "calc.py" in three.failure_detail
    assert "markers" in three.failure_detail or "tests fail" in three.failure_detail
    assert [a.purpose for a in three.attempts] == ["merge"] and three.cost == pytest.approx(0.01)
    # Nothing is left behind: no branch for the task that never started.
    assert not subprocess.run(["git", "branch", "--list", "km0/r1/03"], cwd=repo, capture_output=True,
                              text=True, check=True).stdout.strip()
