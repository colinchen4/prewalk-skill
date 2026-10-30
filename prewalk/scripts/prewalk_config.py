#!/usr/bin/env python3
"""~/.prewalk/config.json (schema 2): which Builder backend/model/effort to use.

No secrets ever live here: logins stay in each official CLI, API keys stay in the
open agent's own config or the user's shell environment.  The legacy
``last-pairing.json`` (prewalk_prefs.py) is migrated into ``pairing`` and left
in place so older skill versions keep working.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import prewalk_prefs

SCHEMA_VERSION = 2
MAX_CONCURRENCY_LIMIT = 2
BUILDER_KEYS = ("backend", "model", "effort", "max_concurrency")
TOP_KEYS = ("schema_version", "builder", "navigator_host", "pairing", "migrated_from", "updated_at")
SECRET_HINTS = ("key", "token", "secret", "password", "credential", "cookie", "auth")


class ConfigError(ValueError):
    pass


def home_dir() -> Path:
    env = os.environ.get("PREWALK_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".prewalk"


def config_path() -> Path:
    env = os.environ.get("PREWALK_CONFIG", "").strip()
    return Path(env).expanduser() if env else home_dir() / "config.json"


def _legacy_pairing_path() -> Path:
    if os.environ.get(prewalk_prefs.PREFS_ENV, "").strip():
        return prewalk_prefs.default_prefs_path()
    return home_dir() / "last-pairing.json"


def _reject_secrets(value: Any, where: str = "config") -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if any(h in str(k).lower() for h in SECRET_HINTS):
                raise ConfigError(f"{where}.{k}: secrets are never stored in Prewalk config")
            _reject_secrets(v, f"{where}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _reject_secrets(v, f"{where}[{i}]")


def validate(cfg: Any) -> dict[str, Any]:
    if not isinstance(cfg, dict):
        raise ConfigError("config must be a JSON object")
    _reject_secrets(cfg)
    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    for k in TOP_KEYS:
        if k in cfg and k != "schema_version":
            out[k] = cfg[k]
    builder = cfg.get("builder") or {}
    if not isinstance(builder, dict):
        raise ConfigError("builder must be an object")
    b = {k: builder[k] for k in BUILDER_KEYS if builder.get(k) is not None}
    for k in ("backend", "model", "effort"):
        if k in b and not isinstance(b[k], str):
            raise ConfigError(f"builder.{k} must be a string")
    mc = b.get("max_concurrency", 1)
    if not isinstance(mc, int) or isinstance(mc, bool) or not 1 <= mc <= MAX_CONCURRENCY_LIMIT:
        raise ConfigError(f"builder.max_concurrency must be 1..{MAX_CONCURRENCY_LIMIT}")
    b["max_concurrency"] = mc
    out["builder"] = b
    if "pairing" in out and out["pairing"] is not None:
        out["pairing"] = prewalk_prefs.validate_pairing(out["pairing"])
    return out


def load(path: Path | None = None) -> tuple[dict[str, Any], str]:
    """Return (config, source).  source: config | migrated | default.
    Never writes (doctor must stay read-only)."""
    path = path or config_path()
    if path.is_file():
        try:
            return validate(json.loads(path.read_text(encoding="utf-8"))), "config"
        except (ValueError, OSError) as exc:
            raise ConfigError(f"{path}: {exc}") from exc
    legacy = _legacy_pairing_path()
    try:
        pairing = prewalk_prefs.load_last_pairing(legacy)
    except prewalk_prefs.PrefsError:
        pairing = None
    if pairing:
        return validate({"builder": {}, "pairing": pairing, "migrated_from": str(legacy)}), "migrated"
    return validate({"builder": {}}), "default"


def save(cfg: dict[str, Any], path: Path | None = None) -> Path:
    path = path or config_path()
    clean = validate(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def max_concurrency() -> int:
    try:
        cfg, _ = load()
    except ConfigError:
        return 1
    return int(cfg.get("builder", {}).get("max_concurrency", 1))
