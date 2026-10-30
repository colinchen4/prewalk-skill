#!/usr/bin/env python3
"""ACP-shaped persistent Builder test double."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

SESSION = f"mock-session-{os.getpid()}"
TRACE = Path(os.environ["PREWALK_MOCK_TRACE"])
TRACE.parent.mkdir(parents=True, exist_ok=True)
STATE: dict[str, Any] = {"session_id": SESSION, "pid": os.getpid(), "prompts": [],
                         "model": "mock-ok", "thinking": "medium", "mode": "default"}


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def trace() -> None:
    tmp = TRACE.with_suffix(".tmp")
    tmp.write_text(json.dumps(STATE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(TRACE)


def result(request_id: Any, payload: dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": payload})


def prompt(text: str) -> None:
    phase = text.splitlines()[0] if text else ""
    STATE["prompts"].append({"phase": phase, "chars": len(text),
                             "sha256": hashlib.sha256(text.encode()).hexdigest()})
    if phase == "PREWALK_PHASE: EXPLORING":
        response = "EXPLORATION_READY\nscope checked; waiting for execute"
    elif phase == "PREWALK_PHASE: EXECUTE":
        if os.environ.get("PREWALK_MOCK_FAIL") == "1":
            raise RuntimeError("mock execution failed")
        target = Path.cwd() / "hello.txt"
        with target.open("a", encoding="utf-8") as handle:
            handle.write("hello from builder\n")
        response = "implementation complete"
    else:
        response = "delta applied"
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": SESSION,
                     "update": {"type": "agent_message_chunk", "text": response}}})
    trace()


def main() -> None:
    trace()
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        params = request.get("params") or {}
        request_id = request.get("id")
        if method == "initialize":
            result(request_id, {"protocolVersion": 1, "agentCapabilities": {}})
        elif method == "session/new":
            result(request_id, {"sessionId": SESSION, "configOptions": []})
        elif method == "session/set_config_option":
            STATE[params["configId"] if params["configId"] != "model" else "model"] = params["value"]
            result(request_id, {"configOptions": []})
            trace()
        elif method == "session/set_mode":
            STATE["mode"] = params["modeId"]
            result(request_id, {})
            trace()
        elif method == "session/prompt":
            prompt(params["prompt"][0]["text"])
            result(request_id, {"stopReason": "end_turn"})
        elif method == "session/close":
            result(request_id, {})
        else:
            send({"jsonrpc": "2.0", "id": request_id,
                  "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
