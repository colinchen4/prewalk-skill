"""Fake `claude` / `codex` / `kimi` binaries on PATH for backend tests.

Behaviour is driven by env vars so one PATH serves every scenario:
  FAKE_CLAUDE_AUTH = subscription | api_key | logged_out
  FAKE_CODEX_AUTH  = chatgpt | api_key | logged_out
  FAKE_CLAUDE_VERSION / FAKE_CODEX_VERSION
Every invocation is appended to $FAKE_LOG so tests can assert probes stay read-only.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

CLAUDE = r'''#!/bin/sh
echo "claude $*" >> "${FAKE_LOG:-/dev/null}"
case "$1" in
  --version) echo "${FAKE_CLAUDE_VERSION:-2.1.310} (Claude Code)"; exit 0;;
  auth)
    case "${FAKE_CLAUDE_AUTH:-subscription}" in
      subscription) echo '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "max"}'; exit 0;;
      api_key) echo '{"loggedIn": true, "authMethod": "api_key"}'; exit 0;;
      *) echo '{"loggedIn": false}'; exit 1;;
    esac;;
esac
exit 0
'''

CODEX = r'''#!/bin/sh
echo "codex $*" >> "${FAKE_LOG:-/dev/null}"
case "$1" in
  --version) echo "codex-cli ${FAKE_CODEX_VERSION:-0.160.0}"; exit 0;;
  login)
    case "${FAKE_CODEX_AUTH:-chatgpt}" in
      chatgpt) echo "Logged in using ChatGPT"; exit 0;;
      api_key) echo "Logged in using an API key - sk-***"; exit 0;;
      *) echo "Not logged in"; exit 1;;
    esac;;
esac
exit 0
'''

KIMI = r'''#!/bin/sh
echo "kimi $*" >> "${FAKE_LOG:-/dev/null}"
case "$1" in
  --version) echo "kimi, version 1.40.0"; exit 0;;
esac
exit 0
'''


def make_bin_dir(root: Path, names=("claude", "codex", "kimi")) -> Path:
    d = root / "bin"
    d.mkdir(parents=True, exist_ok=True)
    for name in names:
        p = d / name
        p.write_text({"claude": CLAUDE, "codex": CODEX, "kimi": KIMI}[name], encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return d


def isolated_env(root: Path, bin_dir: Path | None, **extra: str) -> dict[str, str]:
    """Environment with only fake CLIs on PATH and an empty HOME / PREWALK_HOME."""
    home = root / "home"
    home.mkdir(exist_ok=True)
    path = os.pathsep.join(([str(bin_dir)] if bin_dir else []) + ["/usr/bin", "/bin"])
    env = {"PATH": path, "HOME": str(home), "PREWALK_HOME": str(home / ".prewalk"),
           "PREWALK_CODEX_DEEPSEEK_MANAGER": "none", "FAKE_LOG": str(root / "fake.log"),
           "KIMI_CODE_HOME": str(home / ".kimi-code")}
    env.update(extra)
    return env
