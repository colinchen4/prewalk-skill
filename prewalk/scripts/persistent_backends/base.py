"""Persistent JSON-RPC Builder processes used by the Prewalk supervisor."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import prewalk_hash
import prewalk_runtime
from builder_backends.base import child_env
from builder_backends.process import popen_group_kwargs

BUILDER_ENV_REMOVE = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_*", "CLAUDE_CODE_EFFORT_LEVEL", "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "OPENAI_MODEL", "CODEX_API_KEY", "DEEPSEEK_API_KEY", "KIMI_API_KEY", "KIMI_CODE_API_KEY",
    "KIMI_MODEL", "KIMI_DEFAULT_MODEL", "KIMI_CODE_MODEL", "KIMI_CODE_DEFAULT_MODEL",
)


class PersistentError(RuntimeError):
    pass


class CancelledError(PersistentError):
    pass


def _env_seconds(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def binary_integrity(path: str) -> dict[str, Any]:
    target = Path(path).resolve()
    if not target.is_file():
        raise PersistentError(f"Builder executable is missing: {target}")
    return {"path": str(target), "sha256": prewalk_hash.sha256_file(target),
            "size": target.stat().st_size}


class JsonRpcBackend:
    def __init__(self, argv: list[str], cwd: Path, run_dir: Path,
                 env: dict[str, str] | None = None, integrity: dict[str, Any] | None = None):
        self.cwd = Path(cwd)
        self.run_dir = Path(run_dir)
        self.integrity = integrity or {}
        self.events_path = self.run_dir / "builder-events.jsonl"
        self.err_path = self.run_dir / "builder.err"
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self._events = self.events_path.open("a", encoding="utf-8")
        self._err = self.err_path.open("ab")
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self.session_id: str | None = None
        self.last_text = ""
        self.config_options: list[dict[str, Any]] = []
        self.proc = subprocess.Popen(argv, cwd=str(self.cwd), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self._err,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1,
                                     env=env, **popen_group_kwargs())
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                with self._write_lock:
                    self._events.write(json.dumps({"direction": "agent", "message": message},
                                                   ensure_ascii=False) + "\n")
                    self._events.flush()
                self._messages.put(message)
        finally:
            self._events.close()
            self._messages.put({"type": "prewalk.process_closed", "returncode": self.proc.poll()})

    def _send(self, message: dict[str, Any]) -> None:
        with self._write_lock:
            self._events.write(json.dumps({"direction": "navigator", "message": message},
                                           ensure_ascii=False) + "\n")
            self._events.flush()
            assert self.proc.stdin is not None
            try:
                self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise PersistentError(f"Builder stdin is closed: {exc}") from exc

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _update_text(self, message: dict[str, Any]) -> str | None:
        if message.get("method") != "session/update":
            return None
        params = message.get("params") or {}
        if self.session_id and params.get("sessionId") not in (None, self.session_id):
            return None
        update = params.get("update") or {}
        if update.get("sessionUpdate") == "agent_message_chunk":
            content = update.get("content") or {}
            text = content.get("text")
        elif update.get("type") == "agent_message_chunk":
            text = update.get("text")
        else:
            text = None
        return text if isinstance(text, str) else None

    def _cancel_request(self, method: str, request_id: int) -> None:
        try:
            self.notify("session/cancel", {"sessionId": self.session_id})
        except PersistentError:
            pass
        grace = _env_seconds("PREWALK_CANCEL_GRACE", 15.0)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                message = self._messages.get(timeout=min(0.5, deadline - time.monotonic()))
            except queue.Empty:
                continue
            if message.get("type") == "prewalk.process_closed" or message.get("id") == request_id:
                break
        if self.proc.poll() is None:
            self.proc.kill()
        raise CancelledError(f"{method} cancelled by Navigator")

    def call(self, method: str, params: dict[str, Any], timeout: float = 600,
             idle_timeout: float | None = None,
             cancel_check: Any = None) -> dict[str, Any]:
        with self._write_lock:
            self._next_id += 1
            request_id = self._next_id
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        self._send(request)
        effective = idle_timeout if idle_timeout is not None else timeout
        deadline = time.monotonic() + effective
        self.last_text = ""
        chunks: list[str] = []
        while True:
            if cancel_check is not None and cancel_check():
                self._cancel_request(method, request_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PersistentError(f"Builder request timed out: {method}")
            try:
                message = self._messages.get(timeout=min(0.5, remaining))
            except queue.Empty:
                continue
            deadline = time.monotonic() + effective
            text = self._update_text(message)
            if text:
                chunks.append(text)
            if message.get("type") == "prewalk.process_closed":
                self.last_text = "".join(chunks)
                raise PersistentError(f"Builder exited during {method}: {message.get('returncode')}")
            if message.get("id") != request_id:
                continue
            if "error" in message:
                self.last_text = "".join(chunks)
                raise PersistentError(f"{method}: {message['error']}")
            self.last_text = "".join(chunks)
            return message.get("result") or {}

    def start(self, model: str | None = None, effort: str | None = None) -> str:
        self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        result = self.call("session/new", {"cwd": str(self.cwd), "mcpServers": [],
                                           "additionalDirectories": [str(self.run_dir)]})
        self.session_id = result.get("sessionId")
        if not self.session_id:
            raise PersistentError("session/new returned no sessionId")
        self.config_options = result.get("configOptions") or []
        self.configure(model, effort)
        self.call("session/set_mode", {"sessionId": self.session_id, "modeId": "auto"})
        return self.session_id

    def configure(self, model: str | None = None, effort: str | None = None) -> None:
        if not self.session_id:
            raise PersistentError("Builder session has not started")
        if model:
            result = self.call("session/set_config_option", {"sessionId": self.session_id,
                                                             "configId": "model", "value": model})
            self.config_options = result.get("configOptions") or self.config_options
        if effort:
            result = self.call("session/set_config_option", {"sessionId": self.session_id,
                                                             "configId": "thinking", "value": effort})
            self.config_options = result.get("configOptions") or self.config_options

    def current_config(self) -> dict[str, Any]:
        return {option.get("id"): option.get("currentValue")
                for option in self.config_options if option.get("id")}

    def verify_integrity(self) -> None:
        if not self.integrity:
            raise PersistentError("Builder executable integrity is unavailable")
        current = binary_integrity(self.integrity["path"])
        if current["sha256"] != self.integrity["sha256"] or current["size"] != self.integrity["size"]:
            raise PersistentError("Builder executable changed during the run")

    def prompt(self, text: str, cancel_check: Any = None) -> dict[str, Any]:
        if not self.session_id:
            raise PersistentError("Builder session has not started")
        result = self.call("session/prompt",
                           {"sessionId": self.session_id,
                            "prompt": [{"type": "text", "text": text}]},
                           idle_timeout=_env_seconds("PREWALK_BUILDER_IDLE_TIMEOUT", 1800.0),
                           cancel_check=cancel_check)
        return {"result": result, "text": self.last_text}

    def close(self) -> None:
        if self.session_id and self.proc.poll() is None:
            try:
                self.call("session/close", {"sessionId": self.session_id}, timeout=5)
            except PersistentError:
                pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._err.close()
        if self.proc.stdin:
            self.proc.stdin.close()


def create_backend(backend_id: str, cwd: Path, run_dir: Path,
                   model: str | None, effort: str | None) -> JsonRpcBackend:
    if backend_id == "mock":
        script = Path(__file__).resolve().with_name("mock_persistent_builder.py")
        env = child_env(os.environ, BUILDER_ENV_REMOVE,
                        {"PREWALK_MOCK_TRACE": str(run_dir / "mock-session.json")})
        return JsonRpcBackend([sys.executable, str(script)], cwd, run_dir, env,
                              integrity=binary_integrity(str(script)))
    if backend_id == "kimi-sub":
        import shutil

        executable = shutil.which("kimi") or "kimi"
        return JsonRpcBackend([executable, "acp"], cwd, run_dir,
                              child_env(os.environ, BUILDER_ENV_REMOVE),
                              integrity=binary_integrity(executable))
    raise PersistentError(f"{backend_id}: persistent Builder is not implemented; use the legacy adapter")
