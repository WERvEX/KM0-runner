import json
import subprocess
import sys
from pathlib import Path

import pytest

from km0_runner.pi_client import PiClient, pi_argv
from km0_runner.tiers import PiConfig, RunnerConfig

FAKE_PI = Path(__file__).with_name("fake_pi.py")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    (path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    subprocess.run("git init -q && git add -A && git -c user.email=t@t -c user.name=t commit -qm init",
                   shell=True, cwd=path, check=True)
    return path


def ladder(**overrides) -> RunnerConfig:
    data = {
        "pi": {"command": [sys.executable, str(FAKE_PI)]},
        "ladder": [
            {"name": "T1", "model": "fake/cheap", "step_limit": 10, "cost_limit": 1.0},
            {"name": "T2", "model": "fake/mid", "step_limit": 10, "cost_limit": 1.0},
            {"name": "T3", "model": "fake/strong", "step_limit": 10, "cost_limit": 1.0},
        ],
    }
    data.update(overrides)
    return RunnerConfig.model_validate(data)


class FakePi:
    """Writes the fake pi's script and starts it like the Runner starts pi."""

    def __init__(self, tmp_path: Path, runs: dict, price: dict | None = None):
        self.script = tmp_path / "script.json"
        self.log = tmp_path / "commands.jsonl"
        self.script.write_text(json.dumps({"runs": runs, "price": price or {}}))

    def __call__(self, rung, workspace, log):
        argv, _ = pi_argv(PiConfig(command=[sys.executable, str(FAKE_PI)]), rung, workspace, None)
        env = {"FAKE_PI_SCRIPT": str(self.script), "FAKE_PI_LOG": str(self.log)}
        log.parent.mkdir(parents=True, exist_ok=True)
        return PiClient(argv, workspace, env, log).start()

    def commands(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def prompts(self) -> list[tuple[str, str]]:
        return [(c["model"], c["message"]) for c in self.commands() if c["type"] == "prompt"]


def bash(command):
    return {"tools": [{"name": "bash", "args": {"command": command}}]}


def edit(path, old, new):
    return {"tools": [{"name": "edit", "args": {"path": path, "oldText": old, "newText": new}}]}


def done(text="done"):
    return {"text": text}
