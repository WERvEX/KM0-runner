"""Live progress for whoever started the run (KM0, in practice), and a way to stop the run cleanly.

`progress.jsonl` in the run directory gets one JSON object per line, appended and flushed as things happen, so
a reader can follow it while the run goes on and pick it up again after a restart:

    run_started       run_id, project_id, tasks [{task_id, title}], ladder [{name, model}]
    task_started      task_id, title, tier, explored
    attempt_started   task_id, tier, model, purpose (task | merge)
    attempt_finished  task_id, tier, model, purpose, cost, steps, stop_reason
    escalation        task_id, from_tier, to_tier, signal, detail
    test_result       task_id, passed, detail
    merge_resolution  task_id, files, resolved
    task_finished     task_id, status, solved_tier, cost, failure_category, failure_detail
    run_finished      status (completed | cancelled), passed, unjudged, failed, blocked, cancelled, cost

Every line also carries `seq` (from 1) and `at` (UTC, ISO 8601). A task's status is one of passed, unjudged
(no test command: KM0 decides), failed, blocked or cancelled.

A stop is requested with SIGTERM or SIGINT (POSIX) or by creating `STOP` in the run directory (any platform;
Windows has no SIGTERM to send). The Runner then aborts pi, commits what the current task has so far, reports
it and every task not yet run as cancelled, removes the worktrees and writes the summary.
"""

from __future__ import annotations

import json
import signal
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

STOP_FILE = "STOP"
PROGRESS_FILE = "progress.jsonl"


class Progress:
    """Appends events to `progress.jsonl`; with no path it does nothing."""

    def __init__(self, path: Path | None = None):
        self._file: TextIO | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("a", encoding="utf-8")
        self._seq = 0
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: Any) -> None:
        if self._file is None:
            return
        with self._lock:
            self._seq += 1
            record = {"seq": self._seq, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event}
            self._file.write(json.dumps({**record, **fields}, ensure_ascii=False) + "\n")
            self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


class StopRequest:
    """Whether someone asked the run to stop: by a signal, by `request()`, or by the stop file existing."""

    def __init__(self, stop_file: Path | None = None):
        self.stop_file = stop_file
        self._requested = threading.Event()

    def request(self) -> None:
        self._requested.set()

    @property
    def requested(self) -> bool:
        if not self._requested.is_set() and self.stop_file is not None and self.stop_file.exists():
            self._requested.set()
        return self._requested.is_set()

    def install_signal_handlers(self) -> Callable[[], None]:
        """Turn SIGTERM and SIGINT into a stop request; returns a function that restores the old handlers.
        Only works on the main thread; elsewhere it does nothing."""
        if threading.current_thread() is not threading.main_thread():
            return lambda: None
        previous: dict[int, Any] = {}
        for name in ("SIGTERM", "SIGINT"):
            number = getattr(signal, name, None)
            if number is not None:
                previous[number] = signal.signal(number, lambda *_: self.request())

        def restore() -> None:
            for number, handler in previous.items():
                signal.signal(number, handler)

        return restore


NEVER = StopRequest()
"""A stop request nobody makes, for callers that cannot stop."""
