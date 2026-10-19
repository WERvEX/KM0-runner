"""End to end with the real pi, driven against the scripted bridge. Skipped when pi is not installed."""

import json
import os
import shutil
import socket

import pytest
from test_cascade import CHECK, branch_file, write_package

from km0_runner.handoff import load_km0_package
from km0_runner.llm_bridge import models_json, serve
from km0_runner.pi_client import pi_version
from km0_runner.runner import run_package
from km0_runner.tiers import RunnerConfig

PI = os.environ.get("KM0_TEST_PI") or shutil.which("pi")
pytestmark = pytest.mark.skipif(not PI, reason="pi is not installed (set KM0_TEST_PI to its path)")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_escalation_through_real_pi(tmp_path, repo):
    write_package(repo, [{}], commit=True)
    port = free_port()
    answers = [
        # T1 edits the wrong thing and stops; the test command fails, so the Runner escalates.
        {"tool_calls": [{"name": "write", "arguments": {"path": "notes.txt", "content": "todo\n"}}]},
        {"text": "Done."},
        # T2, in a new session: reads, fixes, runs the tests, reports.
        {"tool_calls": [{"name": "read", "arguments": {"path": "calc.py"}}]},
        {"tool_calls": [{"name": "edit", "arguments": {"path": "calc.py", "edits": [{"oldText": "a - b", "newText": "a + b"}]}}]},
        {"tool_calls": [{"name": "bash", "arguments": {"command": CHECK}}]},
        {"text": "Fixed add()."},
    ]
    script = tmp_path / "answers.jsonl"
    script.write_text("".join(json.dumps(answer) + "\n" for answer in answers))
    server = serve(tmp_path / "bridge", port, script)
    models = tmp_path / "models.json"
    price = {"cheap": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0},
             "strong": {"input": 10, "output": 20, "cacheRead": 0, "cacheWrite": 0}}
    models.write_text(json.dumps(models_json(port, ["cheap", "strong"], price=price)))
    config = RunnerConfig.model_validate({
        "pi": {"command": [PI], "models_json": str(models), "turn_timeout": 120},
        "ladder": [{"name": "T1", "model": "bridge/cheap"}, {"name": "T2", "model": "bridge/strong"}],
    })
    try:
        run_dir, [report] = run_package(load_km0_package(repo), repo, config, tmp_path / "runs", "real",
                                        pi_version=pi_version([PI]), log=lambda line: None)
    finally:
        server.shutdown()

    assert report.pi_version == "1.3.0"
    assert [a.tier for a in report.attempts] == ["T1", "T2"] and report.solved_tier == "T2"
    assert report.escalations[0].signal == "tests_failed"
    assert report.attempts[1].steps == 4 and report.tokens.input > 0 and report.cost > 0
    assert "a + b" in branch_file(repo, "km0/real/01") and branch_file(repo, "km0/real/01", "notes.txt") == "todo\n"
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["top_tier_estimate"] >= summary["cost"]
