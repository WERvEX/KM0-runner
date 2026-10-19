import os
import subprocess

import pytest

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

from minisweagent.models.test_models import DeterministicModel, make_output  # noqa: E402

from km0_runner.tiers import RunnerConfig  # noqa: E402

SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


def step(*commands: str, cost: float = 0.01) -> dict:
    return make_output("THOUGHT", [{"command": command} for command in commands], cost=cost)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    (path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    subprocess.run("git init -q && git add -A && git -c user.email=t@t -c user.name=t commit -qm init",
                   shell=True, cwd=path, check=True)
    return path


def scripted(scripts: dict[str, list[dict]]):
    """A model factory that plays back one script per tier model name."""

    def factory(rung, section):
        return DeterministicModel(outputs=scripts[rung.model_name], observation_template=section["observation_template"])

    return factory


def ladder(**overrides) -> RunnerConfig:
    data = {
        "tiers": {
            "T1": {"model_name": "cheap", "step_limit": 10},
            "T2": {"model_name": "mid", "step_limit": 10},
            "T3": {"model_name": "strong", "step_limit": 10},
        },
    }
    data.update(overrides)
    return RunnerConfig.model_validate(data)
