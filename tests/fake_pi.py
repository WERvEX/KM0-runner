"""A fake `pi --mode rpc`: speaks the same JSONL protocol and plays back a script instead of calling a model.

The script (JSON, path in FAKE_PI_SCRIPT) maps a model id to a list of runs; each prompt sent while that
model is selected plays its next run. A run is a list of turns; a turn is {"tools": [{"name", "args"}]} or
{"text": "..."}. Tools really run in the working directory (bash, write, edit), so tests see real effects.
Every command received is appended to FAKE_PI_LOG. With "shared": true, runs carry on across pi processes. Each assistant message costs `price[model]` USD.
"""

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

script = json.loads(Path(os.environ["FAKE_PI_SCRIPT"]).read_text())
log = Path(os.environ["FAKE_PI_LOG"])
model = next(arg for arg in sys.argv if "/" in arg and sys.argv[sys.argv.index(arg) - 1] == "--model").split("/", 1)[1]
played: dict[str, int] = {}
stats = {"cost": 0.0, "input": 0, "output": 0}
commands: queue.Queue = queue.Queue()
out_lock = threading.Lock()


def emit(record):
    with out_lock:
        # Unicode separators inside strings must survive: the client splits on LF only.
        sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def reader():
    for line in sys.stdin:
        if line.strip():
            command = json.loads(line)
            with log.open("a") as file:
                file.write(json.dumps({**command, "model": model}) + "\n")
            commands.put(command)
    commands.put(None)


def respond(command, data=None, success=True, error=None):
    record = {"id": command.get("id"), "type": "response", "command": command["type"], "success": success}
    if data is not None:
        record["data"] = data
    if error:
        record["error"] = error
    emit(record)


def run_tool(name, args):
    cwd = Path.cwd()
    if name == "bash":
        result = subprocess.run(["bash", "-c", args["command"]], cwd=cwd, capture_output=True, text=True)
        text = (result.stdout + result.stderr) or "(no output)"
        if result.returncode:
            text += f"\n\nCommand exited with code {result.returncode}"
        return text, result.returncode != 0, {"exit_code": result.returncode}
    if name == "write":
        path = cwd / args["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"])
        return f"Wrote {args['path']}", False, {}
    if name == "edit":
        path = cwd / args["path"]
        text = path.read_text()
        if args["oldText"] not in text:
            return f"Could not find the exact text in {args['path']}.", True, {}
        path.write_text(text.replace(args["oldText"], args["newText"], 1))
        return f"Successfully replaced 1 block(s) in {args['path']}.", False, {}
    return f"unknown tool {name}", True, {}


def assistant(content):
    price = script.get("price", {}).get(model, 0.01)
    stats["cost"] += price
    stats["input"] += 100
    stats["output"] += 10
    usage = {"input": 100, "output": 10, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 110, "cost": {"total": price}}
    message = {"role": "assistant", "content": content, "provider": "fake", "model": model, "usage": usage,
               "stopReason": "toolUse" if any(c["type"] == "toolCall" for c in content) else "stop"}
    emit({"type": "message_start", "message": {**message, "content": []}})
    emit({"type": "message_update", "usage": usage, "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": " "}})
    emit({"type": "message_end", "message": message})
    return message


def aborted():
    """Handle commands that arrived mid-run; True when one of them is an abort."""
    stop = False
    while True:
        try:
            command = commands.get_nowait()
        except queue.Empty:
            return stop
        if command is None:
            sys.exit(0)
        if command["type"] == "abort":
            respond(command)
            stop = True
        else:
            handle(command)


def play(prompt_command):
    runs = script["runs"].get(model, [])
    if script.get("shared"):  # runs are consumed across pi processes: count the prompts already logged
        index = sum(1 for line in log.read_text().splitlines()
                    if json.loads(line)["type"] == "prompt" and json.loads(line)["model"] == model) - 1
    else:
        index = played.get(model, 0)
        played[model] = index + 1
    turns = runs[index] if index < len(runs) else [{"text": "nothing to do"}]
    respond(prompt_command, {"disposition": "started"})
    emit({"type": "agent_start"})
    for number, turn in enumerate(turns):
        emit({"type": "turn_start"})
        if "provider_error" in turn:  # a failed request; pi retries it when `retried` is set
            message = {"role": "assistant", "content": [], "provider": "fake", "model": model, "stopReason": "error",
                       "errorMessage": turn["provider_error"]}
            emit({"type": "message_end", "message": message})
            emit({"type": "turn_end", "message": message, "toolResults": []})
            emit({"type": "auto_retry_start", "attempt": 1, "errorMessage": turn["provider_error"]})
            emit({"type": "auto_retry_end", "success": bool(turn.get("retried")), "attempt": 1,
                  **({} if turn.get("retried") else {"finalError": turn["provider_error"]})})
            if turn.get("retried"):
                continue
            break
        if "text" in turn:
            message = assistant([{"type": "text", "text": turn["text"]}])
            emit({"type": "turn_end", "message": message, "toolResults": []})
            break
        calls = [{"type": "toolCall", "id": f"c{number}-{i}", "name": t["name"], "arguments": t["args"]} for i, t in enumerate(turn["tools"])]
        message = assistant(calls)
        for call in calls:
            if aborted():
                emit({"type": "agent_end", "messages": [], "willRetry": False})
                emit({"type": "agent_settled", "aborted": True})
                return
            emit({"type": "tool_execution_start", "toolCallId": call["id"], "toolName": call["name"], "args": call["arguments"]})
            text, is_error, structured = run_tool(call["name"], call["arguments"])
            result = {"content": [{"type": "text", "text": text}], "structuredContent": structured}
            emit({"type": "tool_execution_end", "toolCallId": call["id"], "toolName": call["name"], "result": result, "isError": is_error})
        emit({"type": "turn_end", "message": message, "toolResults": []})
        if aborted():
            emit({"type": "agent_end", "messages": [], "willRetry": False})
            emit({"type": "agent_settled", "aborted": True})
            return
    emit({"type": "agent_end", "messages": [], "willRetry": False})
    emit({"type": "agent_settled", "aborted": False})


def handle(command):
    global model
    kind = command["type"]
    if kind == "prompt":
        play(command)
    elif kind == "set_model":
        model = command["modelId"]
        respond(command, {"id": model, "provider": command["provider"]})
    elif kind == "new_session":
        stats.update(cost=0.0, input=0, output=0)
        respond(command, {"cancelled": False})
    elif kind == "get_session_stats":
        respond(command, {"tokens": {"input": stats["input"], "output": stats["output"], "cacheRead": 0, "cacheWrite": 0,
                                     "total": stats["input"] + stats["output"]}, "cost": stats["cost"]})
    elif kind == "abort":
        respond(command)
    else:
        respond(command, success=False, error=f"unsupported {kind}")


if "--version" in sys.argv:
    print("1.3.0")
    sys.exit(0)
threading.Thread(target=reader, daemon=True).start()
while True:
    command = commands.get()
    if command is None:
        break
    handle(command)
