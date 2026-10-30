"""Shared types for Prewalk Builder backends (stdlib only).

A backend turns "run the Builder on turn N of this handoff" into a concrete
process description (argv, env overlay, env removals, cwd, stdin) for one
specific CLI.  Backends never read, store, or forward credentials: Track A
backends call the user's own logged-in official CLI, Track B backends rely on
the provider configuration the open agent already owns.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

CANONICAL_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")

# Runtime parser hosts understood by ``prewalk_runtime.py inspect --host``.
# This is the single source of truth; prewalk_runtime and prewalk_async import it.
RUNTIME_HOSTS = ("kimi", "codex", "claude-code", "dsh", "generic")

# Navigator host -> Builder family it pairs with by default.
FAMILY_OF_HOST = {
    "claude-code": "claude-code",
    "codex": "codex",
    "kimi": "kimi",
    "opencode": "opencode",
    "pi": "pi",
}
NAVIGATOR_HOSTS = tuple(FAMILY_OF_HOST) + ("dsh", "generic")

BOOTSTRAP = (
    "PREWALK_PHASE: BUILDER_EXECUTE\n"
    "Your complete handoff package is the file {brief}. Read it first, execute it, and finish "
    "with the PREWALK_PHASE: NAVIGATOR_REVIEW package it requires, using the same HANDOFF_ID."
)
BOOTSTRAP_RESUME = (
    "PREWALK_PHASE: BUILDER_RESUME\n"
    "Continue the same Builder session. The resume delta is the file {brief}. Read it first, apply it, "
    "and finish with the PREWALK_PHASE: NAVIGATOR_REVIEW package, using the same HANDOFF_ID."
)

# Phrases that mean "quota/rate limit hit, retry later" rather than a real failure.
RATE_LIMIT_PATTERNS = (
    re.compile(r"you'?ve hit your [^\n]{0,60}limit", re.I),
    re.compile(r"\brate[ _-]?limit", re.I),
    re.compile(r"\busage limit\b", re.I),
    re.compile(r"\b429\b"),
    re.compile(r"too many requests", re.I),
    re.compile(r"quota (?:exceeded|exhausted)", re.I),
    re.compile(r"resets? (?:at|in) ", re.I),
)


class BackendError(ValueError):
    """Invalid backend selection, model, or launch request."""


class BackendNotImplemented(BackendError):
    """Registered backend whose launch path is not built yet (stub)."""


@dataclass
class Detection:
    installed: bool
    path: str | None = None
    version: str | None = None


@dataclass
class Auth:
    state: str            # ready | needs_login | needs_key | unknown | not_installed
    billing: str          # subscription | api_key | third_party | unknown
    detail: str = ""


@dataclass
class Launch:
    argv: list[str]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)   # overlay; never logged
    env_remove: tuple[str, ...] = ()                     # exact names or PREFIX* patterns
    stdin_path: str | None = None
    model: str | None = None
    requested_effort: str | None = None
    effective_effort: str | None = None
    session: str | None = None
    notes: list[str] = field(default_factory=list)

    def build_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        return child_env(base if base is not None else os.environ, self.env_remove, self.env)

    def describe(self) -> dict[str, Any]:
        """Non-secret record for state.json (env values are never included)."""
        return {"argv": list(self.argv), "cwd": self.cwd, "stdin_path": self.stdin_path,
                "env_overlay_keys": sorted(self.env), "env_remove": list(self.env_remove),
                "model": self.model, "requested_effort": self.requested_effort,
                "effective_effort": self.effective_effort, "session": self.session,
                "notes": list(self.notes)}


def child_env(base: Mapping[str, str], remove: Iterable[str] = (),
              overlay: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(base)
    for name in remove:
        if name.endswith("*"):
            prefix = name[:-1]
            for key in [k for k in env if k.startswith(prefix)]:
                env.pop(key, None)
        else:
            env.pop(name, None)
    env.update(overlay or {})
    return env


def which(name: str) -> str | None:
    return shutil.which(name)


def win_wrap(argv: list[str]) -> list[str]:
    """npm-installed CLIs are .cmd shims on Windows; CreateProcess cannot run them directly."""
    if os.name == "nt" and argv and argv[0].lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/d", "/s", "/c", *argv]
    return argv


def run_probe(argv: list[str], env: Mapping[str, str] | None = None,
              timeout: float = 10) -> tuple[int, str]:
    """Read-only probe of an installed CLI.  Never raises."""
    try:
        p = subprocess.run(win_wrap(argv), capture_output=True, text=True, timeout=timeout,
                           env=dict(env) if env is not None else None,
                           stdin=subprocess.DEVNULL)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    m = _VERSION_RE.search(text or "")
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


def is_rate_limited(text: str, exit_code: int | None = None, retry_exit_codes: Iterable[int] = ()) -> bool:
    if exit_code is not None and exit_code in tuple(retry_exit_codes):
        return True
    return any(p.search(text or "") for p in RATE_LIMIT_PATTERNS)


class Backend:
    id = "base"
    track = "?"                         # "A" subscription CLI | "B" open agent + provider | "-" other
    family = "?"                        # Navigator host family this backend pairs with
    binary = ""
    runtime_host = "generic"            # prewalk_runtime.py inspect --host
    weak_model: str | None = None       # recommended Builder default
    weak_effort: str | None = None
    alt_models: tuple[str, ...] = ()
    blocked_models: tuple[str, ...] = ()    # never allowed as Builder (cost / credits)
    supported_efforts: tuple[str, ...] = ()
    env_remove: tuple[str, ...] = ()
    min_version: str | None = None
    implemented = True                  # False -> registry stub, launch() raises
    listed = True                       # False -> hidden from doctor (test-only backends)
    in_default_chain = True             # False -> only used when explicitly selected
    retry_exit_codes: tuple[int, ...] = ()
    cancel_signals = ("SIGTERM", "SIGKILL")
    validation = "unverified"
    login_hint = ""

    # ------------------------------------------------------------ detection
    def detect(self) -> Detection:
        path = which(self.binary) if self.binary else None
        if not path:
            return Detection(False)
        code, out = run_probe([path, "--version"], env=self.probe_env())
        return Detection(True, path, out.splitlines()[0] if code == 0 and out else None)

    def probe_env(self) -> dict[str, str]:
        """Probes see the same environment the Builder will (API keys stripped)."""
        return child_env(os.environ, self.env_remove)

    def auth_check(self, detection: Detection | None = None) -> Auth:
        return Auth("unknown", "unknown", "no read-only auth probe documented")

    def version_note(self, detection: Detection) -> str | None:
        if not (self.min_version and detection.version):
            return None
        have, need = parse_version(detection.version), parse_version(self.min_version)
        if have and need and have < need:
            return f"{self.binary} {'.'.join(map(str, have))} is older than required {self.min_version}"
        return None

    # ------------------------------------------------------------ models / effort
    def check_model(self, model: str | None) -> str:
        chosen = (model or self.weak_model or "").strip()
        if not chosen:
            raise BackendError(f"{self.id}: a model is required")
        low = chosen.lower()
        for bad in self.blocked_models:
            if low == bad or bad in re.split(r"[\[\]/\-_.: ]+", low):
                raise BackendError(
                    f"{self.id}: model {chosen!r} is not allowed as Builder "
                    f"(blocked: {', '.join(self.blocked_models)}); use {self.weak_model}"
                    + (f" or {', '.join(self.alt_models)}" if self.alt_models else ""))
        return chosen

    def efforts(self, model: str | None = None) -> tuple[str, ...]:
        return self.supported_efforts

    def resolve_effort(self, effort: str | None, model: str | None = None) -> str | None:
        """Return the effort that will actually apply.  Never clamps silently:
        callers record requested and effective side by side."""
        if effort is None:
            return None
        if effort not in CANONICAL_EFFORTS:
            raise BackendError(f"unknown effort {effort!r}; allowed: {', '.join(CANONICAL_EFFORTS)}")
        ok = self.efforts(model)
        if not ok:
            return None
        if effort in ok:
            return effort
        order = CANONICAL_EFFORTS
        below = [e for e in ok if order.index(e) <= order.index(effort)]
        return below[-1] if below else ok[0]

    # ------------------------------------------------------------ launch
    def launch(self, *, turn: int, workdir: str, brief_path: str, model: str | None = None,
               effort: str | None = None, session: str | None = None,
               writable_dirs: Iterable[str] = (), allowed_bash: Iterable[str] = ()) -> Launch:
        raise BackendNotImplemented(f"{self.id}: launch is not implemented yet (registry stub)")

    def new_session_id(self) -> str | None:
        """Session id to pre-assign on turn 1, when the CLI accepts one."""
        return None

    def classify_failure(self, exit_code: int | None, text: str) -> str | None:
        """'rate_limited' when the failure is a retryable quota/rate limit."""
        if exit_code == 0:
            return None
        return "rate_limited" if is_rate_limited(text, exit_code, self.retry_exit_codes) else None

    def observed_models(self, events_path: Path) -> list[str]:
        """Models the stream says actually ran (empty when the stream does not say)."""
        return []

    def _exe(self) -> str:
        return which(self.binary) or self.binary

    # ------------------------------------------------------------ doctor row
    def describe(self) -> dict[str, Any]:
        d = self.detect()
        a = self.auth_check(d) if d.installed else Auth("not_installed", "unknown", self.login_hint)
        notes = [n for n in (self.version_note(d),) if n]
        return {"id": self.id, "track": self.track, "family": self.family,
                "implemented": self.implemented, "in_default_chain": self.in_default_chain,
                "installed": d.installed, "path": d.path, "version": d.version,
                "auth": a.state, "billing": a.billing, "auth_detail": a.detail,
                "runtime_host": self.runtime_host, "efforts": list(self.efforts()),
                "weak_model": self.weak_model, "weak_effort": self.weak_effort,
                "alt_models": list(self.alt_models), "blocked_models": list(self.blocked_models),
                "validation": self.validation, "login_hint": self.login_hint, "notes": notes}
