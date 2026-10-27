"""Driving pi in RPC mode: start it, send commands, stream its events, abort, read session stats.

pi speaks strict JSONL on stdin/stdout. Records are split on LF only (never with a line reader that also
breaks on U+2028/U+2029), responses are matched to commands by `id` because they can arrive out of order,
and a prompt's work is over at `agent_settled`, not `agent_end`.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .tiers import PiConfig, Rung

# Flags that keep the target repository's own pi setup (settings, extensions, skills, prompt templates,
# MCP servers) from loading: the Runner decides what pi may run. Context files (AGENTS.md) still load.
ISOLATING_FLAGS = ["--no-approve", "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-mcp", "--offline"]


class PiError(RuntimeError):
    pass


@dataclass
class RunOutcome:
    aborted: bool
    """The Runner stopped the run (a signal or a budget), or pi reported an abort."""
    stop: Any = None
    """What `on_event` returned when it asked to stop."""
    error: str | None = None
    """A provider error pi gave up on, or the process ending early."""


def pi_version(command: list[str]) -> str:
    """`pi --version`, as the bare version number."""
    result = subprocess.run([*command, "--version"], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise PiError(f"`{' '.join(command)} --version` failed: {result.stderr.strip()[-500:]}")
    return result.stdout.strip().split()[-1].lstrip("v")


def agent_dir(config: PiConfig, scratch: Path) -> str | None:
    """The agent directory pi should use: a private one holding only `models_json`, the configured one, or None."""
    if config.models_json:
        private = scratch / "pi-agent"
        private.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(config.models_json).expanduser(), private / "models.json")
        return str(private)
    return str(Path(config.agent_dir).expanduser()) if config.agent_dir else None


def pi_argv(
    config: PiConfig,
    rung: Rung,
    workspace: Path,
    agent: str | None,
    docker_image: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """The command line and extra environment that start pi on `rung` in `workspace`."""
    args = [
        "--mode", "rpc", "--no-session", *ISOLATING_FLAGS,
        "--tools", ",".join(config.tools), "--model", rung.model, *config.extra_args,
    ]  # fmt: skip
    if docker_image:
        # pi itself runs in the container, so its tools only see the mounted workspace.
        mounts = ["-v", f"{workspace}:/workspace", "-w", "/workspace"]
        env_args: list[str] = []
        if agent:
            mounts += ["-v", f"{agent}:/pi-agent:ro"]
            env_args = ["-e", "PI_CODING_AGENT_DIR=/pi-agent"]
        command = ["docker", "run", "-i", "--rm", "--network", "host", *mounts, *env_args, docker_image, "pi"]
        return [*command, *args], {}
    return [*config.command, *args], ({"PI_CODING_AGENT_DIR": agent} if agent else {})


class PiClient:
    def __init__(self, argv: list[str], cwd: str | Path, env: dict[str, str] | None = None, log: Path | None = None):
        self.argv = argv
        self.cwd = str(cwd)
        # Bytecode from the agent's own test runs can shadow its next same-size edit; keep the tree clean too.
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
        self.log = log
        self.process: subprocess.Popen[bytes] | None = None
        self.events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._stderr: Any = None

    # process lifecycle

    def start(self) -> PiClient:
        self._stderr = open(self.log, "ab") if self.log else tempfile.TemporaryFile()  # noqa: SIM115
        self.process = subprocess.Popen(
            self.argv, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr
        )
        threading.Thread(target=self._read, daemon=True).start()
        return self

    def close(self, timeout: float = 10) -> None:
        """Close stdin, which asks pi to shut down; kill it if it does not."""
        if not self.process:
            return
        try:
            if self.process.stdin:
                self.process.stdin.close()
            self.process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            self.process.kill()
            self.process.wait()
        finally:
            if self._stderr:
                self._stderr.close()

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def __enter__(self) -> PiClient:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def stderr_tail(self, chars: int = 2000) -> str:
        if not self.log or not self.log.exists():
            return ""
        return self.log.read_bytes()[-chars:].decode("utf-8", "replace")

    def _read(self) -> None:
        assert self.process and self.process.stdout
        buffer = b""
        stream = self.process.stdout
        while chunk := stream.read1(65536):
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                self._dispatch(line.rstrip(b"\r"))
        if buffer.strip():
            self._dispatch(buffer)
        self.events.put(None)  # end of stream
        with self._lock:
            for waiter in self._pending.values():
                waiter.put({"type": "response", "success": False, "error": "pi exited"})

    def _dispatch(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if record.get("type") == "response" and record.get("id") in self._pending:
            with self._lock:
                waiter = self._pending.pop(record["id"])
            waiter.put(record)
        elif record.get("type") != "response":
            self.events.put(record)

    # commands

    def send(self, command: dict[str, Any]) -> queue.Queue[dict[str, Any]]:
        """Write one command; the returned queue receives its response."""
        assert self.process and self.process.stdin
        command = {"id": f"r{next(self._ids)}", **command}
        waiter: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._pending[command["id"]] = waiter
        try:
            self.process.stdin.write(json.dumps(command, ensure_ascii=False).encode("utf-8") + b"\n")
            self.process.stdin.flush()
        except OSError as error:
            with self._lock:
                self._pending.pop(command["id"], None)
            raise PiError(f"pi is not running: {error}; stderr: {self.stderr_tail()}") from error
        return waiter

    def request(self, type: str, timeout: float = 120, **fields: Any) -> Any:
        try:
            response = self.send({"type": type, **fields}).get(timeout=timeout)
        except queue.Empty:
            raise PiError(f"pi did not answer {type} within {timeout}s") from None
        if not response.get("success"):
            raise PiError(f"pi rejected {type}: {response.get('error')}; stderr: {self.stderr_tail(500)}")
        return response.get("data")

    def set_model(self, rung: Rung) -> None:
        self.request("set_model", provider=rung.provider, modelId=rung.model_id)

    def new_session(self) -> None:
        data = self.request("new_session") or {}
        if data.get("cancelled"):
            raise PiError("pi refused to start a new session")

    def stats(self) -> dict[str, Any]:
        return self.request("get_session_stats") or {}

    def run(
        self,
        message: str,
        on_event: Callable[[dict[str, Any]], Any],
        timeout: float = 3600,
        should_stop: Callable[[], bool] | None = None,
    ) -> RunOutcome:
        """Send a prompt and feed every event to `on_event` until `agent_settled`.

        When `on_event` returns something truthy the run is aborted, and that value comes back as `stop`.
        `should_stop` is asked at least once a second, events or not; when it says yes the run is aborted with
        stop `cancelled`.
        """
        while not self.events.empty():  # events of an earlier run
            if self.events.get_nowait() is None:
                return RunOutcome(aborted=True, error="pi exited")
        disposition = (self.request("prompt", message=message) or {}).get("disposition")
        if disposition == "handled":
            return RunOutcome(aborted=False)
        deadline = time.monotonic() + timeout
        stop: Any = None
        error: str | None = None
        while True:
            try:
                event = self.events.get(timeout=min(1.0, max(0.1, deadline - time.monotonic())))
            except queue.Empty:
                event = {}
            if stop is None and should_stop is not None and should_stop():
                stop = "cancelled"
                self.send({"type": "abort"})
                deadline = time.monotonic() + 120  # give the abort time to settle
            if event is None:
                return RunOutcome(aborted=True, stop=stop, error=error or f"pi exited: {self.stderr_tail(500)}")
            if not event:
                # The queue wakes up every second; only a passed deadline means something.
                if time.monotonic() < deadline:
                    continue
                if stop is not None:
                    return RunOutcome(aborted=True, stop=stop, error="pi did not settle after abort")
                stop = "timeout"
                self.send({"type": "abort"})
                deadline = time.monotonic() + 120  # give the abort time to settle
                continue
            kind = event.get("type")
            if kind == "auto_retry_end":
                # A request pi retried successfully is not the run's error; one it gave up on is.
                error = None if event.get("success") else event.get("finalError") or error
            if kind == "message_end" and (event.get("message") or {}).get("role") == "assistant":
                error = _assistant_error(event["message"])
            if kind == "agent_settled":
                return RunOutcome(aborted=bool(event.get("aborted")) or stop is not None, stop=stop, error=error)
            if stop is None:
                wanted = on_event(event)
                if wanted:
                    stop = wanted
                    self.send({"type": "abort"})


def _assistant_error(message: dict[str, Any]) -> str | None:
    if message.get("role") != "assistant" or message.get("stopReason") != "error":
        return None
    return message.get("errorMessage") or "provider error"


def tool_text(result: Any) -> str:
    """The text of a tool result, as the model saw it."""
    if not isinstance(result, dict):
        return str(result or "")
    parts = [part.get("text", "") for part in result.get("content") or [] if isinstance(part, dict)]
    return "\n".join(part for part in parts if part)


def tool_exit_code(result: Any) -> int | None:
    """A bash result's exit code, when pi reported one."""
    if isinstance(result, dict):
        for key in ("structuredContent", "details"):
            value = (result.get(key) or {}).get("exit_code")
            if isinstance(value, int):
                return value
    return None
