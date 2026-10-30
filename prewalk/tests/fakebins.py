"""Fake `claude` / `codex` / `kimi` binaries on PATH for backend tests.

Behaviour is driven by env vars so one PATH serves every scenario:
  FAKE_CLAUDE_AUTH = subscription | api_key | logged_out
  FAKE_CODEX_AUTH  = chatgpt | api_key | logged_out
  FAKE_CLAUDE_VERSION / FAKE_CODEX_VERSION
Every invocation is appended to $FAKE_LOG so tests can assert probes stay read-only.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

FAKE_CLI = r'''
import os
import sys

backend, args = sys.argv[1], sys.argv[2:]
with open(os.environ.get("FAKE_LOG") or os.devnull, "a", encoding="utf-8") as log:
    log.write(backend + " " + " ".join(args) + "\n")
if backend == "claude":
    if args == ["--version"]:
        print(os.environ.get("FAKE_CLAUDE_VERSION", "2.1.310") + " (Claude Code)")
        raise SystemExit(0)
    if args[:1] == ["auth"]:
        auth = os.environ.get("FAKE_CLAUDE_AUTH", "subscription")
        if auth == "subscription":
            print('{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "max"}')
            raise SystemExit(0)
        if auth == "api_key":
            print('{"loggedIn": true, "authMethod": "api_key"}')
            raise SystemExit(0)
        print('{"loggedIn": false}')
        raise SystemExit(1)
elif backend == "codex":
    if args == ["--version"]:
        print("codex-cli " + os.environ.get("FAKE_CODEX_VERSION", "0.160.0"))
        raise SystemExit(0)
    if args[:1] == ["login"]:
        auth = os.environ.get("FAKE_CODEX_AUTH", "chatgpt")
        if auth == "chatgpt":
            print("Logged in using ChatGPT")
            raise SystemExit(0)
        if auth == "api_key":
            print("Logged in using an API key - sk-***")
            raise SystemExit(0)
        print("Not logged in")
        raise SystemExit(1)
elif backend == "kimi" and args == ["--version"]:
    print("kimi, version 1.40.0")
raise SystemExit(0)
'''


def make_bin_dir(root: Path, names=("claude", "codex", "kimi")) -> Path:
    d = root / "bin"
    d.mkdir(parents=True, exist_ok=True)
    script = d / "fake_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    for name in names:
        if os.name == "nt":
            p = d / f"{name}.cmd"
            p.write_text(f'@echo off\r\n"{sys.executable}" "{script}" {name} %*\r\n', encoding="utf-8")
        else:
            p = d / name
            p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" {name} "$@"\n', encoding="utf-8")
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return d


def isolated_env(root: Path, bin_dir: Path | None, **extra: str) -> dict[str, str]:
    """Environment with only fake CLIs on PATH and an empty HOME / PREWALK_HOME."""
    home = root / "home"
    home.mkdir(exist_ok=True)
    if os.name == "nt":
        empty = root / "empty-bin"
        empty.mkdir(exist_ok=True)
        parts = [str(bin_dir or empty)]
        git = shutil.which("git")
        if git:
            parts.append(str(Path(git).parent))
        path = os.pathsep.join(parts)
    else:
        path = os.pathsep.join(([str(bin_dir)] if bin_dir else []) + ["/usr/bin", "/bin"])
    env = {"PATH": path, "HOME": str(home), "PREWALK_HOME": str(home / ".prewalk"),
           "PREWALK_CODEX_DEEPSEEK_MANAGER": "none", "FAKE_LOG": str(root / "fake.log"),
           "KIMI_CODE_HOME": str(home / ".kimi-code"),
           "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
           "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows")}
    env.update(extra)
    return env
