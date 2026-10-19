from km0_runner.signals import SignalMonitor, is_test_command
from km0_runner.tiers import SignalConfig


def test_is_test_command():
    assert is_test_command("cd /testbed && python -m pytest tests/test_a.py -x")
    assert is_test_command("pnpm test")
    assert is_test_command("PYTHONPATH=. timeout 60 pytest -q")
    assert is_test_command("go test ./...")
    assert not is_test_command("cat pytest.ini")
    assert not is_test_command("grep -r pytest setup.cfg")


def bash(monitor, command, is_error=False, output=""):
    args = {"command": command}
    return monitor.tool_start("bash", args) or monitor.tool_end("bash", args, is_error, output)


def test_test_fail_streak_resets_on_pass():
    monitor = SignalMonitor(SignalConfig(test_fail_streak=2))
    assert bash(monitor, "pytest", True) is None
    assert bash(monitor, "pytest -x") is None
    assert bash(monitor, "pytest -k a", True) is None
    signal = bash(monitor, "pytest -k b", True)
    assert signal and signal.name == "test_fail_streak"


def test_piped_test_run_counts_as_failure_from_its_output():
    monitor = SignalMonitor(SignalConfig(test_fail_streak=2))
    assert bash(monitor, "python -m pytest -q 2>&1 | tail -n 5", output="8 failed, 2 passed in 0.1s") is None
    signal = bash(monitor, "pytest -q | tail", output="FAILED tests/test_a.py::test_x - assert 1 == 2")
    assert signal and signal.name == "test_fail_streak"


def test_repeat_needs_consecutive_identical_calls():
    monitor = SignalMonitor(SignalConfig(repeat_command=3))
    assert bash(monitor, "ls") is None
    assert bash(monitor, "ls  ") is None
    assert bash(monitor, "pwd") is None
    assert bash(monitor, "ls") is None
    assert monitor.tool_start("read", {"path": "a.py"}) is None
    assert monitor.tool_start("read", {"path": "a.py"}) is None
    assert monitor.tool_start("read", {"path": "a.py"}).name == "repeat_command"


def test_same_file_edits_from_edit_and_write_paths():
    monitor = SignalMonitor(SignalConfig(same_file_edits=3, repeat_command=99))
    assert monitor.tool_start("edit", {"path": "a.py", "edits": [1]}) is None
    assert monitor.tool_start("write", {"path": "a.py", "content": "x"}) is None
    assert monitor.tool_start("edit", {"path": "b.py", "edits": [2]}) is None
    signal = monitor.tool_start("edit", {"path": "a.py", "edits": [3]})
    assert signal and signal.name == "same_file_edits" and "a.py" in signal.detail


def test_tool_error_streak():
    monitor = SignalMonitor(SignalConfig(tool_error_streak=2, repeat_command=99))
    assert monitor.tool_end("edit", {"path": "a"}, True) is None
    assert monitor.tool_end("read", {"path": "b"}, False) is None
    assert monitor.tool_end("edit", {"path": "a"}, True) is None
    assert monitor.tool_end("bash", {"command": "x"}, True).name == "tool_error_streak"
