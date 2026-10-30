"""Prewalk Builder backend registry.

    from builder_backends import REGISTRY, get, listed, recommend
    b = get("claude-code-sub"); spec = b.launch(turn=1, workdir=wt, brief_path=brief)

Track A = official CLIs on the user's subscription login, weak model.
Track B = open agents on an external API (stubs in phase 1).
"""

from __future__ import annotations

from typing import Any, Iterable

from .base import (BOOTSTRAP, BOOTSTRAP_RESUME, CANONICAL_EFFORTS, FAMILY_OF_HOST, NAVIGATOR_HOSTS,
                   RUNTIME_HOSTS, Auth, Backend, BackendError, BackendNotImplemented, Detection,
                   Launch, child_env, is_rate_limited)
from .claude_code_sub import ClaudeCodeSub
from .codex_deepseek_legacy import CodexDeepseekLegacy
from .codex_sub import CodexSub
from .kimi_sub import KimiSub
from .mock import MockBackend
from .stubs import OpenCodeApi, PiApi

# Order matters: it is the cross-family fallback order (Track A first).
REGISTRY: dict[str, Backend] = {b.id: b for b in (
    ClaudeCodeSub(), CodexSub(), KimiSub(), OpenCodeApi(), PiApi(), CodexDeepseekLegacy(), MockBackend(),
)}
ALIASES = {"codex:deepseek": "codex-deepseek", "claude": "claude-code-sub",
           "claude-code": "claude-code-sub", "codex": "codex-sub", "kimi": "kimi-sub"}

FALLBACK = """No Builder backend is ready. Pick one:
  A) Use your subscription (recommended):
     claude auth login            # Claude Pro/Max  -> Builder: sonnet
     codex login                  # ChatGPT Plus/Pro -> Builder: gpt-6-luna
     kimi   then /login           # Kimi membership  -> Builder: kimi-for-coding
  B) Use an external API with an open agent (phase 2):
     pi:        export DEEPSEEK_API_KEY=...
     opencode:  opencode auth login --provider deepseek
Then run: prewalk doctor"""


def get(backend_id: str) -> Backend:
    key = ALIASES.get(backend_id, backend_id)
    if key not in REGISTRY:
        known = ", ".join(b.id for b in REGISTRY.values() if b.listed)
        raise BackendError(f"unknown Builder backend {backend_id!r}; known: {known}")
    return REGISTRY[key]


def listed() -> list[Backend]:
    return [b for b in REGISTRY.values() if b.listed]


def family_of_host(host: str | None) -> str | None:
    return FAMILY_OF_HOST.get(host or "")


def recommend(rows: Iterable[dict[str, Any]], navigator_host: str | None,
              saved: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Pairing rule (design §7):
    1. saved config whose backend is still ready;
    2. same family as the Navigator host, ready;
    3. another ready Track A backend, registry order;
    4. a ready, implemented Track B backend;
    5. None -> caller prints FALLBACK.
    Legacy / hidden backends are never auto-picked."""
    rows = list(rows)
    by_id = {r["id"]: r for r in rows}

    def usable(r: dict[str, Any] | None) -> bool:
        if not r or not r["implemented"] or not r["installed"] or r["auth"] != "ready":
            return False
        return r["track"] != "A" or r.get("billing") == "subscription"

    def pick(r: dict[str, Any], reason: str, model=None, effort=None) -> dict[str, Any]:
        return {"backend": r["id"], "model": model or r["weak_model"],
                "effort": effort if effort is not None else r["weak_effort"], "reason": reason}

    if saved and saved.get("backend"):
        r = by_id.get(ALIASES.get(saved["backend"], saved["backend"]))
        if r is None and saved["backend"] in REGISTRY:
            r = REGISTRY[saved["backend"]].describe()
        if usable(r):
            return pick(r, "saved config", saved.get("model"), saved.get("effort"))
    chain = [r for r in rows if r.get("in_default_chain", True)]
    fam = family_of_host(navigator_host)
    for r in chain:
        if fam and r["family"] == fam and r["track"] == "A" and usable(r):
            return pick(r, f"same family as Navigator host {navigator_host}")
    for r in chain:
        if r["track"] == "A" and usable(r):
            return pick(r, "first ready subscription CLI (cross-family)")
    for r in chain:
        if r["track"] == "B" and usable(r):
            return pick(r, "open agent on external API")
    return None


__all__ = ["REGISTRY", "ALIASES", "FALLBACK", "get", "listed", "recommend", "family_of_host",
           "Backend", "Detection", "Auth", "Launch", "BackendError", "BackendNotImplemented",
           "BOOTSTRAP", "BOOTSTRAP_RESUME", "CANONICAL_EFFORTS", "RUNTIME_HOSTS", "FAMILY_OF_HOST",
           "NAVIGATOR_HOSTS", "child_env", "is_rate_limited"]
