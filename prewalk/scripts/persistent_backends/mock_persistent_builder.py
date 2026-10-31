#!/usr/bin/env python3
"""ACP-shaped persistent Builder test double.

Env knobs:
  PREWALK_MOCK_FAIL        prompt raises instead of replying (run fails)
  PREWALK_MOCK_SLOW        seconds a prompt takes; session/cancel interrupts it
  PREWALK_MOCK_STREAM      number of reply chunks, sent ~0.4 s apart
  PREWALK_MOCK_CONTRADICT  exploration reply gains this CONTRADICTION: line
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

SESSION = f"mock-session-{os.getpid()}"
TRACE = Path(os.environ["PREWALK_MOCK_TRACE"])
TRACE.parent.mkdir(parents=True, exist_ok=True)
STATE: dict[str, Any] = {"session_id": SESSION, "pid": os.getpid(), "prompts": [],
                         "model": "mock-ok", "thinking": "medium", "mode": "default",
                         "leaked_env": sorted(key for key in (
                             "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                             "DEEPSEEK_API_KEY", "KIMI_API_KEY", "KIMI_MODEL") if key in os.environ)}
INBOX: queue.Queue[dict[str, Any] | None] = queue.Queue()
CANCELLED = threading.Event()


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def trace() -> None:
    tmp = TRACE.with_suffix(".tmp")
    tmp.write_text(json.dumps(STATE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(TRACE)


def config_options() -> list[dict[str, str]]:
    return [{"id": "model", "currentValue": STATE["model"]},
            {"id": "thinking", "currentValue": STATE["thinking"]}]


def result(request_id: Any, payload: dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": payload})


def chunk(text: str) -> None:
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": SESSION,
                     "update": {"sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": text}}}})


def stream(text: str) -> None:
    count = int(os.environ.get("PREWALK_MOCK_STREAM") or "1")
    if count <= 1:
        chunk(text)
        return
    pieces = [text[i:i + 8] for i in range(0, len(text), 8)] or [""]
    for index in range(count):
        chunk(pieces[index % len(pieces)])
        if index + 1 < count:
            time.sleep(0.4)


def slow() -> bool:
    """Sleep out PREWALK_MOCK_SLOW; True when session/cancel interrupted it."""
    delay = float(os.environ.get("PREWALK_MOCK_SLOW") or "0")
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        if CANCELLED.is_set():
            return True
        time.sleep(min(0.1, deadline - time.monotonic()))
    return CANCELLED.is_set()


def reply_text(phase: str) -> str:
    if phase == "PREWALK_PHASE: EXPLORING":
        text = "EXPLORATION_READY\nscope checked; waiting for execute"
        contradict = os.environ.get("PREWALK_MOCK_CONTRADICT")
        if contradict:
            text += f"\nCONTRADICTION: {contradict}"
        return text
    if phase == "PREWALK_PHASE: EXECUTE":
        target = Path.cwd() / "hello.txt"
        with target.open("a", encoding="utf-8") as handle:
            handle.write("hello from builder\n")
        return ("implementation complete\n"
                "NOTE: appended hello from builder to hello.txt\n"
                "all plan constraints were respected")
    return "delta applied\nNOTE: delta noted"


def prompt(request_id: Any, text: str) -> None:
    current = next((line for line in text.splitlines() if line.startswith("CURRENT_PHASE:")), "")
    phase = current.removeprefix("CURRENT_PHASE:").strip()
    phase = f"PREWALK_PHASE: {phase}" if phase else ""
    STATE["prompts"].append({"phase": phase, "chars": len(text), "goal": "GOAL:" in text,
                             "sha256": hashlib.sha256(text.encode()).hexdigest()})
    trace()
    if phase == "PREWALK_PHASE: EXECUTE" and os.environ.get("PREWALK_MOCK_FAIL") == "1":
        raise RuntimeError("mock execution failed")
    if slow() or CANCELLED.is_set():
        result(request_id, {"stopReason": "cancelled"})
        return
    stream(reply_text(phase))
    result(request_id, {"stopReason": "end_turn"})


def handle(request: dict[str, Any]) -> None:
    method = request.get("method")
    params = request.get("params") or {}
    request_id = request.get("id")
    if method == "initialize":
        result(request_id, {"protocolVersion": 1, "agentCapabilities": {}})
    elif method == "session/new":
        result(request_id, {"sessionId": SESSION, "configOptions": config_options()})
    elif method == "session/set_config_option":
        STATE[params["configId"] if params["configId"] != "model" else "model"] = params["value"]
        result(request_id, {"configOptions": config_options()})
        trace()
    elif method == "session/set_mode":
        STATE["mode"] = params["modeId"]
        result(request_id, {})
        trace()
    elif method == "session/prompt":
        prompt(request_id, params["prompt"][0]["text"])
    elif method == "session/cancel":
        CANCELLED.set()
        STATE["cancelled"] = True
        trace()
    elif method == "session/close":
        result(request_id, {})
    elif request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id,
              "error": {"code": -32601, "message": f"unknown method {method}"}})


def reader() -> None:
    for line in sys.stdin:
        try:
            INBOX.put(json.loads(line))
        except json.JSONDecodeError:
            continue
    INBOX.put(None)


def main() -> None:
    trace()
    threading.Thread(target=reader, daemon=True).start()
    while True:
        request = INBOX.get()
        if request is None:
            return
        handle(request)


if __name__ == "__main__":
    main()
