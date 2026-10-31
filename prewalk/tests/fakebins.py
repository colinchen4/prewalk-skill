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
elif backend == "kimi":
    if args == ["--version"]:
        print("kimi, version 1.40.0")
    elif args[:2] == ["provider", "list"]:
        if os.environ.get("FAKE_KIMI_AUTH") == "oauth":
            print("managed:kimi-code  type=kimi  models=6  source=oauth")
        else:
            print("deepseek  type=openai  models=4  source=inline")
    elif args[:2] == ["session", "list"]:
        listing = os.environ.get("FAKE_KIMI_SESSION_LIST")
        if listing:
            with open(listing, encoding="utf-8") as handle:
                print(handle.read())
        else:
            print("[]")
elif backend == "ctags":
    import json as _json
    import re as _re
    if "--version" in args:
        print("Universal Ctags 6.1.0(fake)" if os.environ.get("FAKE_CTAGS", "universal") == "universal"
              else "Exuberant Ctags 5.8")
        raise SystemExit(0)
    files = [a for i, a in enumerate(args)
             if not a.startswith("-") and (i == 0 or args[i - 1] not in ("-f", "-o"))]
    tag = _re.compile(r"^\s*(?:export\s+)?(?:async\s+)?"
                      r"(?:function|class|interface|type|def|fun)\s+([A-Za-z_$][\w$]*)")
    for name in files:
        try:
            lines = open(name, encoding="utf-8").read().splitlines()
        except OSError:
            continue
        hits = [(n, tag.match(line).group(1)) for n, line in enumerate(lines, 1) if tag.match(line)]
        for i, (n, sym) in enumerate(hits):
            end = hits[i + 1][0] - 1 if i + 1 < len(hits) else len(lines)
            print(_json.dumps({"_type": "tag", "name": sym, "path": name,
                               "line": n, "kind": "function", "end": end}))
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
