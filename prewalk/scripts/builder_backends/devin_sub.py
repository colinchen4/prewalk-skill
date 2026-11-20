"""devin-sub: Devin CLI persistent ACP Builder on SWE-2 / SWE-2 Flash.

VERIFIED (Devin CLI 3000.11.234 / 3000.14.30825, 2026-11-20):
  - `devin acp` speaks JSON-RPC 2.0 ACP over stdio with `initialize`, `session/new`,
    `session/set_config_option`, `session/set_mode`, `session/prompt`, `session/cancel`,
    and `session/close`.
  - When invoked from inside Devin Desktop, `ACP_BACKEND=windsurf` is inherited in the
    shell environment and causes `devin` CLI to ignore stored CLI credentials. Stripping
    `ACP_BACKEND` (and `WINDSURF_API_KEY`) restores stored `devin auth login` OAuth auth.
  - `devin auth status` exits 0 even when logged out; readiness checks the output text.
  - `devin acp` exposes `configOptions` with `id="model"` (family representative slug,
    e.g. `swe-2-high` for SWE-2 and `swe-2-flash-medium` for SWE-2 Flash),
    `id="thought_level"` (`medium`, `high`, `max`), and `id="mode"` (`bypass`).
  - Usage is streamed via `session/update` (`usage_update`) and `_cognition.ai/turn_stats`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .base import (BOOTSTRAP, BOOTSTRAP_RESUME, CANONICAL_EFFORTS, Auth, Backend, BackendError,
                   Detection, Launch, run_probe, which)

_EFFORT_SUFFIX_RE = re.compile(
    r"^(?P<family>.+?)-(?P<effort>" + "|".join(CANONICAL_EFFORTS) + r")$"
)
_SLUG_SEP_RE = re.compile(r"[\s_]+")
_MULTI_DASH_RE = re.compile(r"-+")
_FAMILY_ALIASES = {"swe": "swe-2"}


def find_devin_binary() -> str | None:
    """Locate the `devin` CLI binary, honoring DEVIN_BIN and Desktop fallback on Windows."""
    override = os.environ.get("DEVIN_BIN", "").strip()
    if override:
        return override
    found = which("devin")
    if found:
        return found
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app:
            candidate = (Path(local_app) / "Programs" / "Devin" / "resources" / "app"
                         / "extensions" / "windsurf" / "devin" / "bin" / "devin.exe")
            if candidate.is_file():
                return str(candidate)
    return None


def split_model_effort(model: str | None) -> tuple[str, str | None]:
    """Normalize a Devin model slug or label (e.g. 'SWE-2 Flash Medium') into (family, effort)."""
    if not model:
        return "", None
    slug = _MULTI_DASH_RE.sub("-", _SLUG_SEP_RE.sub("-", model.strip().lower())).strip("-")
    slug = _FAMILY_ALIASES.get(slug, slug)
    match = _EFFORT_SUFFIX_RE.match(slug)
    if match:
        family = _FAMILY_ALIASES.get(match.group("family"), match.group("family"))
        return family, match.group("effort")
    return slug, None


def effective_model_slug(acp_model: str | None, thought_level: str | None = None) -> str | None:
    """Combine a Devin ACP family model option with its active thought_level."""
    if not acp_model:
        return None
    family, default_effort = split_model_effort(acp_model)
    effort = (thought_level or "").strip().lower() or default_effort
    return f"{family}-{effort}" if effort else family


def resolve_acp_model_option(model: str | None, options: list[dict[str, Any]],
                             effort: str | None = None) -> tuple[str | None, str | None]:
    """Map a requested Devin model + effort to an accepted ACP `model` option and `thought_level`."""
    if not model:
        return None, effort
    values = [opt["value"] for opt in options
              if isinstance(opt, dict) and isinstance(opt.get("value"), str)]
    req_family, req_suffix = split_model_effort(model)
    target_effort = effort if effort is not None else req_suffix
    if model in values:
        return model, target_effort
    want_lightning = req_family.endswith("-lightning")
    for val in values:
        val_family, _ = split_model_effort(val)
        if not want_lightning and val_family.endswith("-lightning"):
            continue
        if val_family == req_family:
            return val, target_effort
    return model, target_effort


def models_match(requested_model: str | None, requested_effort: str | None,
                 observed_model: str | None, observed_effort: str | None = None) -> bool:
    """Return True when observed Devin model/effort matches what was requested."""
    if not requested_model or not observed_model:
        return False
    req_family, req_suffix = split_model_effort(requested_model)
    obs_family, obs_suffix = split_model_effort(observed_model)
    if not req_family or req_family != obs_family:
        return False
    obs_eff = (observed_effort or "").strip().lower() or obs_suffix
    expected_eff = (requested_effort or "").strip().lower() or req_suffix
    if expected_eff and obs_eff and obs_eff != expected_eff:
        return False
    return True


class DevinSub(Backend):
    id, track, family, binary, runtime_host = "devin-sub", "A", "devin", "devin", "devin"
    weak_model, weak_effort = "swe-2-high", "medium"
    alt_models = ("swe-2-flash-medium",)
    blocked_models = (
        "opus", "fable", "sol", "astra", "neon", "argon", "carbon", "hydrogen",
        "fusion", "adaptive", "lightning", "pro", "ultra",
    )
    supported_efforts = ("medium", "high", "max")
    env_remove = (
        "ACP_BACKEND", "DEVIN_MODEL", "DEVIN_PERMISSION_MODE", "DEVIN_REFUSAL_FALLBACK",
        "DEVIN_VERBOSITY", "WINDSURF_API_KEY", "WINDSURF_IDE_TYPE", "WINDSURF_EXT_HOST_PID",
    )
    persistent = True
    validation = "persistent ACP and Devin subscription OAuth live-verified"
    login_hint = "devin auth login   (sign in with your Devin account)"

    def _exe(self) -> str:
        return find_devin_binary() or self.binary

    def detect(self) -> Detection:
        path = find_devin_binary()
        if not path:
            return Detection(False)
        code, out = run_probe([path, "--version"], env=self.probe_env())
        return Detection(True, path, out.splitlines()[0] if code == 0 and out else None)

    def auth_check(self, detection: Detection | None = None) -> Auth:
        exe = (detection.path if detection else None) or self._exe()
        code, out = run_probe([exe, "auth", "status"], env=self.probe_env())
        low = out.lower()
        if code != 0 or "not logged in" in low or "logged in" not in low:
            return Auth("needs_login", "unknown", "run: " + self.login_hint)
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        first = lines[0] if lines else "Logged in"
        plan = next((line for line in lines if line.lower().startswith(("plan:", "tier:"))), "")
        detail = f"{first}; {plan}" if plan else first
        billing = "api_key" if ("api key" in low and "via devin" not in low) else "subscription"
        return Auth("ready", billing, detail)

    def launch(self, *, turn, workdir, brief_path, model=None, effort=None, session=None,
               writable_dirs=(), allowed_bash=()) -> Launch:
        model = self.check_model(model)
        requested = effort or self.weak_effort
        eff = self.resolve_effort(requested, model)
        effective = effective_model_slug(model, eff) or model
        argv = [self._exe()]
        if turn > 1:
            if not session:
                raise BackendError("devin-sub: resume requires the recorded session id")
            argv += ["--resume", session]
        prompt = (BOOTSTRAP if turn == 1 else BOOTSTRAP_RESUME).format(brief=brief_path)
        argv += ["--model", effective, "--permission-mode", "dangerous",
                 "--respect-workspace-trust", "false", "-p", prompt]
        notes = []
        if requested and eff != requested:
            notes.append(f"effort {requested} not supported by {model}; using {eff}")
        return Launch(argv, cwd=str(workdir), env_remove=self.env_remove, model=effective,
                      requested_effort=requested, effective_effort=eff, session=session, notes=notes)
