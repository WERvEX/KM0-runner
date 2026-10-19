from km0_runner.signals import SignalMonitor, edited_files, is_test_command
from km0_runner.tiers import SignalConfig


def test_edited_files():
    assert edited_files("sed -i 's/a/b/' src/x.py") == ["src/x.py"]
    assert edited_files("cat <<'EOF' > src/y.py\nprint(1)\nEOF") == ["src/y.py"]
    assert edited_files("echo hi >> notes.md && grep x a.py 2>&1") == ["notes.md"]
    assert edited_files("python run.py > /tmp/out.txt 2>&1") == []
    assert edited_files("ls > /dev/null") == []
    assert edited_files("printf x | tee -a log.txt") == ["log.txt"]


def test_is_test_command():
    assert is_test_command("cd /testbed && python -m pytest tests/test_a.py -x")
    assert is_test_command("pnpm test")
    assert is_test_command("PYTHONPATH=. timeout 60 pytest -q")
    assert is_test_command("go test ./...")
    assert not is_test_command("cat pytest.ini")
    assert not is_test_command("grep -r pytest setup.cfg")


def test_test_fail_streak_resets_on_pass():
    monitor = SignalMonitor(SignalConfig(test_fail_streak=2), step_limit=0)
    assert monitor.observe("pytest", 1) is None
    assert monitor.observe("pytest -x", 0) is None
    assert monitor.observe("pytest -k a", 1) is None
    signal = monitor.observe("pytest -k b", 1)
    assert signal and signal.name == "test_fail_streak"


def test_repeat_command_needs_consecutive_runs():
    monitor = SignalMonitor(SignalConfig(repeat_command=3), step_limit=0)
    assert monitor.observe("ls", 0) is None
    assert monitor.observe("ls  ", 0) is None
    assert monitor.observe("pwd", 0) is None
    assert monitor.observe("ls", 0) is None


def test_step_budget():
    monitor = SignalMonitor(SignalConfig(), step_limit=2)
    assert monitor.start_step() is None
    assert monitor.start_step() is None
    assert monitor.start_step().name == "step_budget"
