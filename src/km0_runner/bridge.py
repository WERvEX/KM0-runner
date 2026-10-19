"""File bridge: a model that writes each request to a file and waits for someone to write the answer.

For testing without an API key: whoever watches the bridge directory (for example a fresh Claude
subagent per request) reads `NNNN.request.json` and writes the reply text to `NNNN.response.md`.
The reply uses the text-based action format (one ```mswea_bash_command block), so pair this model
with mini-swe-agent's `mini_textbased.yaml`. Answers are cached by request content, so a re-run
reuses the answers for requests that did not change. There is no token count, so cost is 0.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from minisweagent.models.utils.actions_text import format_observation_messages, parse_regex_actions
from pydantic import BaseModel

_LOCK = threading.Lock()


class FileBridgeModelConfig(BaseModel):
    model_name: str
    """Who should answer, e.g. `bridge/haiku`; written into each request."""
    bridge_dir: str = os.getenv("KM0_BRIDGE_DIR", "bridge")
    poll_seconds: float = 1.0
    timeout_seconds: float = 1800
    action_regex: str = r"```mswea_bash_command\s*\n(.*?)\n```"
    format_error_template: str = "Please always provide EXACTLY ONE action in triple backticks, found {{actions|length}} actions."
    observation_template: str = "<returncode>{{output.returncode}}</returncode>\n<output>\n{{output.output}}</output>"
    multimodal_regex: str = ""
    model_kwargs: dict[str, Any] = {}


def request_key(model_name: str, messages: list[dict]) -> str:
    slim = [{"role": m.get("role"), "content": m.get("content")} for m in messages]
    payload = json.dumps({"model": model_name, "messages": slim}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


class FileBridgeModel:
    def __init__(self, **kwargs):
        self.config = FileBridgeModelConfig(**kwargs)
        self.dir = Path(self.config.bridge_dir)
        (self.dir / "cache").mkdir(parents=True, exist_ok=True)

    def _next_number(self) -> int:
        with _LOCK:
            counter = self.dir / "counter"
            number = int(counter.read_text()) + 1 if counter.exists() else 1
            counter.write_text(str(number))
            return number

    def _answer(self, messages: list[dict]) -> str:
        key = request_key(self.config.model_name, messages)
        cached = self.dir / "cache" / f"{key}.md"
        if cached.exists():
            return cached.read_text(encoding="utf-8")
        number = self._next_number()
        request = self.dir / f"{number:04d}.request.json"
        response = self.dir / f"{number:04d}.response.md"
        slim = [{"role": m.get("role"), "content": m.get("content")} for m in messages]
        body = {"number": number, "model": self.config.model_name, "key": key, "messages": slim}
        request.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        deadline = time.time() + self.config.timeout_seconds
        while not response.exists():
            if time.time() > deadline:
                raise TimeoutError(f"no answer for {request} after {self.config.timeout_seconds}s")
            time.sleep(self.config.poll_seconds)
        time.sleep(0.2)  # let the writer finish
        text = response.read_text(encoding="utf-8")
        cached.write_text(text, encoding="utf-8")
        return text

    def query(self, messages: list[dict], **kwargs) -> dict:
        content = self._answer(messages)
        actions = parse_regex_actions(
            content, action_regex=self.config.action_regex, format_error_template=self.config.format_error_template
        )
        return {
            "role": "assistant",
            "content": content,
            "extra": {"actions": actions, "cost": 0.0, "timestamp": time.time()},
        }

    def format_message(self, **kwargs) -> dict:
        return kwargs

    def format_observation_messages(self, message: dict, outputs: list[dict], template_vars: dict | None = None):
        return format_observation_messages(
            outputs,
            observation_template=self.config.observation_template,
            template_vars=template_vars,
            multimodal_regex=self.config.multimodal_regex,
        )

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return self.config.model_dump()

    def serialize(self) -> dict:
        return {
            "info": {
                "config": {
                    "model": self.config.model_dump(mode="json"),
                    "model_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                }
            }
        }
