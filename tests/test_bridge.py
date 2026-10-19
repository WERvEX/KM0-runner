import json
import threading
import time

from conftest import SUBMIT

from km0_runner.bridge import FileBridgeModel
from km0_runner.cascade import Cascade
from km0_runner.handoff import RunTask
from km0_runner.judge import CommandJudge
from km0_runner.tiers import RunnerConfig

ANSWERS = [
    "Fix the operator.\n\n```mswea_bash_command\nsed -i 's/a - b/a + b/' calc.py\n```",
    f"Done.\n\n```mswea_bash_command\n{SUBMIT}\n```",
]


def answer_requests(bridge, stop):
    """Plays whoever watches the bridge: answers each request file in order."""
    answered = 0
    while not stop.is_set() and answered < len(ANSWERS):
        request = bridge / f"{answered + 1:04d}.request.json"
        if request.exists():
            assert json.loads(request.read_text())["model"] == "bridge/t1"
            (bridge / f"{answered + 1:04d}.response.md").write_text(ANSWERS[answered])
            answered += 1
        time.sleep(0.05)


def run(tmp_path, repo, bridge):
    config = RunnerConfig.model_validate({
        "tiers": {"T1": {"model_name": "bridge/t1", "model_class": "km0_runner.bridge.FileBridgeModel",
                         "model_kwargs": {}}},
        "agent_config": "mini_textbased.yaml",
        "environment": {"cwd": str(repo), "environment_class": "local"},
    })

    def factory(rung, section):
        return FileBridgeModel(**section, model_name=rung.model_name, bridge_dir=str(bridge), poll_seconds=0.05)

    task = RunTask(task_id="01", title="t", prompt="Make add() add.", predicted_tier="T1", source="km0")
    judge = CommandJudge("python -c 'import calc; assert calc.add(2, 3) == 5'")
    return Cascade(config, tmp_path / "out", judge=judge, model_factory=factory).run(task)


def test_bridge_round_trip_and_cache(tmp_path, repo):
    bridge = tmp_path / "bridge"
    stop = threading.Event()
    worker = threading.Thread(target=answer_requests, args=(bridge, stop))
    worker.start()
    try:
        report = run(tmp_path, repo, bridge)
    finally:
        stop.set()
        worker.join()
    assert report.solved_tier == "T1" and report.steps == 2

    # A re-run with the same requests is answered from the cache, without new request files.
    import subprocess
    subprocess.run("git checkout -q . && git clean -fdq", shell=True, cwd=repo, check=True)
    report = run(tmp_path / "again", repo, bridge)
    assert report.solved_tier == "T1"
    assert sorted(p.name for p in bridge.glob("*.request.json")) == ["0001.request.json", "0002.request.json"]
