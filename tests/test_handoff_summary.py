import json

from km0_runner.handoff import load_km0_package, load_swebench_instances
from km0_runner.report import ExecutionReport, TestResult
from km0_runner.summary import summarize

TASKS = [
    {"seq": 2, "title": "B", "tier": "T2", "prompt": "km0/tasks/02.md", "dependsOn": [1], "acceptance": ["b"],
     "features": {"risks": ["security"]}, "score": 4, "testCommand": "pytest -q"},
    {"seq": 1, "title": "A", "tier": "T1", "prompt": "km0/tasks/01.md"},
]
PACKAGE = {
    "projectId": 1,
    "files": [
        {"path": "km0/tasks.json", "content": json.dumps(TASKS)},
        {"path": "km0/tasks/01.md", "content": "do A"},
        {"path": "km0/tasks/02.md", "content": "do B"},
    ],
    "tasks": [],
}


def test_package_json_and_folder_agree(tmp_path):
    path = tmp_path / "package.json"
    path.write_text(json.dumps(PACKAGE))
    from_json = load_km0_package(path)
    assert from_json.project_id == 1
    assert [(t.task_id, t.predicted_tier, t.prompt) for t in from_json.tasks] == [("01", "T1", "do A"), ("02", "T2", "do B")]
    b = from_json.tasks[1]
    assert b.depends_on == ["01"] and b.acceptance == ["b"] and b.test_command == "pytest -q" and b.risky
    assert b.predicted_score == 4
    for folder in (from_json.root, from_json.root.parent):  # km0/ itself, or the folder holding it
        assert [t.model_dump() for t in load_km0_package(folder).tasks] == [t.model_dump() for t in from_json.tasks]


def test_swebench_instances(tmp_path):
    path = tmp_path / "i.jsonl"
    path.write_text('{"instance_id": "a__b-1", "problem_statement": "x"}\n{"instance_id": "c__d-2", "problem_statement": "y"}\n')
    tasks = load_swebench_instances(path, predicted={"c__d-2": "T3"}, ids=["c__d-2"])
    assert [(t.task_id, t.predicted_tier, t.source) for t in tasks] == [("c__d-2", "T3", "swebench")]


def report(task_id, predicted, solved, cost):
    return ExecutionReport(
        run_id="r", task_id=task_id, source="swebench", predicted_tier=predicted, start_tier=predicted, solved_tier=solved,
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
