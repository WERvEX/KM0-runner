import json

from km0_runner.handoff import load_km0_package, load_swebench_instances, unpack_km0_package
from km0_runner.report import ExecutionReport, TestResult
from km0_runner.summary import summarize

PACKAGE = {
    "projectId": 1,
    "createdAt": "2026-10-19T00:00:00Z",
    "root": "km0",
    "files": [
        {"path": "km0/tasks.json", "content": json.dumps([
            {"seq": 2, "title": "B", "tier": "T2", "prompt": "km0/tasks/02.md"},
            {"seq": 1, "title": "A", "tier": "T1", "prompt": "km0/tasks/01.md"},
        ])},
        {"path": "km0/tasks/01.md", "content": "do A"},
        {"path": "km0/tasks/02.md", "content": "do B"},
    ],
    "tasks": [
        {"taskId": 11, "seq": 1, "title": "A", "tier": "T1", "agent": "generic", "model": None,
         "promptPath": "km0/tasks/01.md", "prompt": "do A", "command": None},
        {"taskId": 12, "seq": 2, "title": "B", "tier": "T2", "agent": "generic", "model": None,
         "promptPath": "km0/tasks/02.md", "prompt": "do B", "command": None},
    ],
    "agents": [],
}


def test_package_json_and_unpacked_repo_agree(tmp_path):
    path = tmp_path / "package.json"
    path.write_text(json.dumps(PACKAGE))
    unpack_km0_package(path, tmp_path / "repo")
    from_json = load_km0_package(path)
    from_repo = load_km0_package(tmp_path / "repo")
    assert [(t.task_id, t.predicted_tier, t.prompt) for t in from_json] == [("01", "T1", "do A"), ("02", "T2", "do B")]
    assert [t.model_dump() for t in from_json] == [t.model_dump() for t in from_repo]


def test_swebench_instances(tmp_path):
    path = tmp_path / "i.jsonl"
    path.write_text('{"instance_id": "a__b-1", "problem_statement": "x"}\n{"instance_id": "c__d-2", "problem_statement": "y"}\n')
    tasks = load_swebench_instances(path, predicted={"c__d-2": "T3"}, ids=["c__d-2"])
    assert [(t.task_id, t.predicted_tier, t.source) for t in tasks] == [("c__d-2", "T3", "swebench")]


def report(task_id, predicted, solved, cost):
    return ExecutionReport(
        task_id=task_id, source="swebench", predicted_tier=predicted, start_tier=predicted, solved_tier=solved,
        cost=cost, steps=10, wall_time=1.0, failure_category=None if solved else "tests_failed",
        test_result=TestResult(passed=bool(solved), judge="swebench"), handoff="discard",
        config_hash="abc", runner_version="0.1.0",
    )


def test_summary_confusion():
    summary = summarize([report("a", "T1", "T1", 0.1), report("b", "T1", "T3", 0.5), report("c", "T2", None, 0.3)])
    assert summary.solved == 2 and summary.judged == 3 and summary.solve_rate == 0.6667
    assert summary.confusion["T1"] == {"T1": 1, "T2": 0, "T3": 1, "unsolved": 0}
    assert summary.confusion["T2"]["unsolved"] == 1
    assert summary.failures == {"tests_failed": 1}
