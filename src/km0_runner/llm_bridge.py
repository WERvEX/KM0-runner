"""A stand-in model provider for tests without an API key: an OpenAI-compatible chat-completions server.

pi is pointed at it as a provider in `models.json`. Each request pi makes is written to a folder as
`NNNN.request.md` (readable: system prompt, tools, conversation) and `NNNN.request.json` (raw), and the
server waits for `NNNN.response.json`, which someone else writes: a person, a script, or a fresh model
session that reads only that request. The answer is either text or tool calls:

    {"text": "Done: added the parser and its tests."}
    {"tool_calls": [{"name": "bash", "arguments": {"command": "pytest -q"}}], "text": "optional"}

Answers are cached by request content, so a rerun with unchanged requests needs no new answers. A script
of answers (one JSON object per line) can stand in for the folder, for integration tests.

This tests the Runner and the prompts, not a provider: no token counts but rough estimates, no real costs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Rough per-request usage so budgets and stats move; 4 characters per token.
CHARS_PER_TOKEN = 4


def render(body: dict[str, Any]) -> str:
    """The request as a person or a model would read it. Only messages, tool calls and results: no reasoning."""
    lines = [f"# Model request · model `{body.get('model')}`", ""]
    if not body.get("tools"):
        # KM0's own calls (triage, questions, documents, acceptance) have no tools: the answer is the reply text.
        lines += [
            "Answer as the assistant below would. Write the answer as JSON to the response file named in the",
            'instructions you were given: {"text": "..."}, where the text is exactly the reply the system prompt',
            "asks for (for example a JSON object, as a string).",
        ]
    else:
        lines += [
            "Answer as the coding agent below would: decide its next step. Write the answer as JSON to the response",
            'file named in the instructions you were given: {"tool_calls": [{"name": ..., "arguments": {...}}]} to',
            'call tools, or {"text": "..."} when the task is finished and you report back. One or more tool calls',
            "per answer; they run in order and you see their results in the next request.",
            "",
            "## Tools",
        ]
    for tool in body.get("tools") or []:
        function = tool.get("function", {})
        params = function.get("parameters", {}).get("properties", {})
        required = set(function.get("parameters", {}).get("required", []))
        args = ", ".join(f"{name}{'' if name in required else '?'}: {spec.get('type', 'any')}" for name, spec in params.items())
        lines.append(f"- `{function.get('name')}({args})`: {function.get('description', '').strip()}")
    for message in body.get("messages") or []:
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
        if role == "system":
            lines += ["", "## System prompt", "", content or ""]
        elif role == "user":
            lines += ["", "## User", "", content or ""]
        elif role == "assistant":
            lines += ["", "## Agent"]
            if content:
                lines += ["", content]
            for call in message.get("tool_calls") or []:
                function = call.get("function", {})
                lines += ["", f"Tool call `{function.get('name')}`:", "```json", function.get("arguments", "{}"), "```"]
        elif role == "tool":
            lines += ["", "## Tool result", "```", content or "", "```"]
    return "\n".join(lines) + "\n"


def request_key(body: dict[str, Any]) -> str:
    stable = {key: body.get(key) for key in ("model", "messages", "tools")}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:24]


class Bridge:
    def __init__(self, folder: Path, script: Path | None = None, timeout: float = 3600, poll: float = 0.5):
        self.folder = folder
        self.cache = folder / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.script = [json.loads(line) for line in script.read_text(encoding="utf-8").splitlines() if line.strip()] if script else None
        self.timeout = timeout
        self.poll = poll
        self.lock = threading.Lock()
        self.count = len(list(folder.glob("*.request.json")))

    def answer(self, body: dict[str, Any]) -> dict[str, Any]:
        key = request_key(body)
        cached = self.cache / f"{key}.json"
        with self.lock:
            if self.script is not None:
                return self.script.pop(0) if self.script else {"text": "(script exhausted)"}
            if cached.exists():
                return json.loads(cached.read_text(encoding="utf-8"))
            self.count += 1
            number = f"{self.count:04d}"
        (self.folder / f"{number}.request.json").write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.folder / f"{number}.request.md").write_text(render(body), encoding="utf-8")
        response = self.folder / f"{number}.response.json"
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if response.exists():
                try:
                    answer = json.loads(response.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    time.sleep(self.poll)  # still being written
                    continue
                cached.write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
                return answer
            time.sleep(self.poll)
        return {"text": "(no answer before the bridge timeout)"}


def completion_chunks(body: dict[str, Any], answer: dict[str, Any]) -> list[dict[str, Any]]:
    """The answer as OpenAI streaming chunks: one delta, then the finish reason with usage."""
    calls = answer.get("tool_calls") or []
    delta: dict[str, Any] = {"role": "assistant", "content": answer.get("text") or None}
    if calls:
        delta["tool_calls"] = [
            {
                "index": index, "id": f"call_{uuid_hex()}", "type": "function",
                "function": {"name": call["name"], "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False)},
            }
            for index, call in enumerate(calls)
        ]  # fmt: skip
    prompt_chars = len(json.dumps(body.get("messages") or []))
    usage = {
        "prompt_tokens": prompt_chars // CHARS_PER_TOKEN,
        "completion_tokens": max(1, len(json.dumps(answer)) // CHARS_PER_TOKEN),
    }
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    base = {"id": "bridge", "object": "chat.completion.chunk", "model": body.get("model")}
    return [
        {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if calls else "stop"}], "usage": usage},
    ]


def uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


def serve(folder: Path, port: int = 8765, script: Path | None = None, timeout: float = 3600) -> ThreadingHTTPServer:
    bridge = Bridge(folder, script, timeout)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            chunks = completion_chunks(body, bridge.answer(body))
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                return
            first, last = chunks
            message = dict(first["choices"][0]["delta"])
            reply = {
                "id": "bridge", "object": "chat.completion", "model": body.get("model"),
                "choices": [{"index": 0, "message": message, "finish_reason": last["choices"][0]["finish_reason"]}],
                "usage": last["usage"],
            }  # fmt: skip
            payload = json.dumps(reply, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def models_json(port: int, models: list[str], provider: str = "bridge", price: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    """A pi `models.json` pointing `provider/<model>` at the bridge."""
    return {
        "providers": {
            provider: {
                "baseUrl": f"http://127.0.0.1:{port}/v1",
                "api": "openai-completions",
                "apiKey": "bridge",
                "models": [
                    {"id": model, "cost": (price or {}).get(model, {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0})}
                    for model in models
                ],
            }
        }
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="km0-runner bridge", description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", help="where requests are written and answers are read")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--script", help="JSONL of answers to replay instead of waiting for files")
    parser.add_argument("--timeout", type=float, default=3600, help="seconds to wait for each answer")
    args = parser.parse_args(argv)
    folder = Path(args.folder)
    folder.mkdir(parents=True, exist_ok=True)
    serve(folder, args.port, Path(args.script) if args.script else None, args.timeout)
    print(f"bridge on http://127.0.0.1:{args.port}/v1, requests in {folder}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0
