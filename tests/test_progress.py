import json
import sys
import threading
import time

from conftest import FakePi, bash, done, ladder
from test_cascade import FIX, write_package

from km0_runner.cli import main
from km0_runner.handoff import load_km0_package
from km0_runner.progress import StopRequest
from km0_runner.runner import run_package


def events(run_dir):
    return [json.loads(line) for line in (run_dir / "progress.jsonl").read_text().splitlines()]


def run(tmp_path, repo, runs, stop=None):
    fake = FakePi(tmp_path, runs, price={"cheap": 0.01, "mid": 0.05, "strong": 0.2})
    return run_package(
        load_km0_package(repo), repo, ladder(), tmp_path / "runs", "r1", pi_factory=fake, pi_version="1.3.0",
        log=lambda line: None, **({"stop": stop} if stop else {}),
    )  # fmt: skip


def test_progress_follows_the_run_as_it_happens(tmp_path, repo):
    write_package(repo, [{}, {"dependsOn": [1]}], commit=True)
    runs = {"cheap": [[bash("echo wrong >> notes.txt"), done()], [FIX, done()]], "mid": [[FIX, done()]]}
    run_dir, reports = run(tmp_path, repo, runs)

    log = events(run_dir)
    assert [e["seq"] for e in log] == list(range(1, len(log) + 1)) and all(e["at"].endswith("+00:00") for e in log)
    assert [e["event"] for e in log] == [
        "run_started",
        "task_started", "attempt_started", "attempt_finished", "test_result", "escalation",
        "attempt_started", "attempt_finished", "test_result", "task_finished",
        "task_started", "attempt_started", "attempt_finished", "test_result", "task_finished",
        "run_finished",
    ]  # fmt: skip
    started = log[0]
    assert started["project_id"] == 7 and [t["task_id"] for t in started["tasks"]] == ["01", "02"]
    assert [rung["model"] for rung in started["ladder"]] == ["fake/cheap", "fake/mid", "fake/strong"]
    assert log[2] == {**log[2], "task_id": "01", "tier": "T1", "model": "fake/cheap", "purpose": "task"}
    assert log[4]["passed"] is False and log[5] == {**log[5], "from_tier": "T1", "to_tier": "T2", "signal": "tests_failed"}
    assert log[9] == {**log[9], "task_id": "01", "status": "passed", "solved_tier": "T2", "failure_category": None}
    assert log[9]["cost"] == reports[0].cost
    assert log[-1] == {**log[-1], "status": "completed", "passed": 2, "failed": 0, "cancelled": 0}


def test_a_stop_file_before_the_run_cancels_every_task(tmp_path, repo):
    write_package(repo, [{}, {}], commit=True)
    stop = StopRequest(tmp_path / "runs" / "r1" / "STOP")
    (tmp_path / "runs" / "r1").mkdir(parents=True)
    (tmp_path / "runs" / "r1" / "STOP").touch()
    run_dir, reports = run(tmp_path, repo, {"cheap": [[FIX, done()]]}, stop)

    assert [r.failure_category for r in reports] == ["cancelled", "cancelled"]
    assert all(r.start_tier is None and not r.attempts for r in reports)
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["cancelled"] == 2 and summary["failed"] == 0
    assert events(run_dir)[-1] == {**events(run_dir)[-1], "status": "cancelled", "cancelled": 2}


def test_stopping_mid_task_aborts_pi_keeps_its_work_and_cleans_up(tmp_path, repo):
    write_package(repo, [{}, {"dependsOn": [1]}], commit=True)
    runs = {"cheap": [[FIX, *[bash("sleep 1") for _ in range(20)], done()]]}
    stop = StopRequest()

    def stop_once_started():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            path = tmp_path / "runs" / "r1" / "progress.jsonl"
            if path.exists() and '"attempt_started"' in path.read_text():
                time.sleep(1.5)
                stop.request()
                return
            time.sleep(0.1)

    threading.Thread(target=stop_once_started, daemon=True).start()
    began = time.monotonic()
    run_dir, reports = run(tmp_path, repo, runs, stop)

    assert time.monotonic() - began < 15  # nowhere near the twenty seconds the script would take
    first, second = reports
    assert first.failure_category == "cancelled" and first.attempts[-1].stop_reason == "cancelled"
    assert first.commit and "a + b" in (run_dir / first.diff).read_text()  # the partial work is on its branch
    assert second.failure_category == "cancelled"
    assert not (run_dir / "worktrees" / "01").exists()
    statuses = [(e["task_id"], e["status"]) for e in events(run_dir) if e["event"] == "task_finished"]
    assert statuses == [("01", "cancelled"), ("02", "cancelled")]


def test_the_cli_reports_its_version(capsys):
    try:
        main(["--version"])
    except SystemExit as exit:
        assert exit.code == 0
    assert capsys.readouterr().out.strip() == "km0-runner 0.2.0"


def test_the_cli_exits_3_when_stopped(tmp_path, repo):
    write_package(repo, [{}], commit=True)
    config = tmp_path / "runner.json"
    # Stopped before the first task, pi is only asked its version.
    data = ladder(pi={"command": [sys.executable, "-c", "print('1.3.0')"]}).model_dump(mode="json")
    config.write_text(json.dumps(data))
    (tmp_path / "runs" / "r9").mkdir(parents=True)
    (tmp_path / "runs" / "r9" / "STOP").touch()
    code = main(["run", str(repo), "--repo", str(repo), "-c", str(config), "--runs", str(tmp_path / "runs"), "--run-id", "r9"])
    assert code == 3
    assert json.loads((tmp_path / "runs" / "r9" / "summary.json").read_text())["cancelled"] == 1
