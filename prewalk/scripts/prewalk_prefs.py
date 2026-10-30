#!/usr/bin/env python3
"""Persist and recall Prewalk Navigator/Builder model-effort pairing prefs."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_NAVIGATOR_EFFORT = None
DEFAULT_BUILDER_EFFORT = "medium"
ALLOWED_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})
PREFS_ENV = "PREWALK_PREFS_PATH"
DEFAULT_PREFS_RELATIVE = Path(".prewalk") / "last-pairing.json"
RECOMMENDATION_NOTE = (
    "Navigator uses the current session model and effort; Builder medium follows "
    "the first-edit template — do not default Builder to max."
)


class PrefsError(ValueError):
    """Invalid pairing prefs input or file contents."""


def default_prefs_path() -> Path:
    env = os.environ.get(PREFS_ENV)
    if env is not None and str(env).strip() != "":
        return Path(env).expanduser()
    return Path.home() / DEFAULT_PREFS_RELATIVE


def recommended_defaults() -> dict[str, Any]:
    return {
        "navigator_model": None,
        "navigator_effort": DEFAULT_NAVIGATOR_EFFORT,
        "builder_model": None,
        "builder_effort": DEFAULT_BUILDER_EFFORT,
        "note": RECOMMENDATION_NOTE,
    }


def normalize_effort(value: str, *, role: str) -> str:
    cleaned = (value or "").strip().lower()
    if cleaned not in ALLOWED_EFFORTS:
        allowed = ", ".join(sorted(ALLOWED_EFFORTS))
        raise PrefsError(f"invalid {role} effort {value!r}; allowed: {allowed}")
    return cleaned


def validate_pairing(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PrefsError("pairing must be a JSON object")
    navigator_model = raw.get("navigator_model")
    builder_model = raw.get("builder_model")
    if navigator_model is not None and not isinstance(navigator_model, str):
        raise PrefsError("navigator_model must be a string or null")
    if builder_model is not None and not isinstance(builder_model, str):
        raise PrefsError("builder_model must be a string or null")
    navigator_effort = (
        normalize_effort(str(raw["navigator_effort"]), role="navigator")
        if raw.get("navigator_effort") else None
    )
    builder_effort = normalize_effort(
        str(raw.get("builder_effort") or DEFAULT_BUILDER_EFFORT),
        role="builder",
    )
    pairing = {
        "navigator_model": navigator_model.strip() if isinstance(navigator_model, str) else None,
        "navigator_effort": navigator_effort,
        "builder_model": builder_model.strip() if isinstance(builder_model, str) else None,
        "builder_effort": builder_effort,
        "note": RECOMMENDATION_NOTE,
    }
    if pairing["navigator_model"] == "":
        pairing["navigator_model"] = None
    if pairing["builder_model"] == "":
        pairing["builder_model"] = None
    return pairing


def load_last_pairing(path: Path | None = None) -> dict[str, Any] | None:
    target = path or default_prefs_path()
    if not target.is_file():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrefsError(f"cannot read pairing prefs at {target}: {exc}") from exc
    return validate_pairing(raw)


def save_last_pairing(pairing: dict[str, Any], path: Path | None = None) -> Path:
    target = path or default_prefs_path()
    validated = validate_pairing(pairing)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(validated, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".last-pairing.", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def format_reuse_line(pairing: dict[str, Any]) -> str:
    nav_model = pairing.get("navigator_model") or "current-parent"
    nav_effort = pairing["navigator_effort"] or "current-session"
    bld_model = pairing.get("builder_model") or "adapter-default"
    return (
        f"reuse last: {nav_model}/{nav_effort} "
        f"→ {bld_model}/{pairing['builder_effort']}"
    )


def format_preflight_prompt(last: dict[str, Any] | None = None) -> str:
    defaults = recommended_defaults()
    lines = [
        "Prewalk model/effort preflight",
        f"Recommended: Navigator effort {defaults['navigator_effort'] or 'current-session'}; "
        f"Builder effort {defaults['builder_effort']}.",
        RECOMMENDATION_NOTE,
        "Accept 'use defaults' for all four, or answer each question.",
    ]
    if last is not None:
        lines.insert(1, format_reuse_line(last))
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        help=f"prefs file (default ~/.prewalk/last-pairing.json or ${PREFS_ENV})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="print last pairing or recommended defaults")
    show.add_argument("--json", action="store_true")

    defaults = sub.add_parser("defaults", help="print recommended defaults")
    defaults.add_argument("--json", action="store_true")

    prompt = sub.add_parser("prompt", help="print preflight prompt text")
    prompt.add_argument("--json", action="store_true")

    save = sub.add_parser("save", help="save a confirmed pairing")
    save.add_argument("--navigator-model")
    save.add_argument("--navigator-effort", default=DEFAULT_NAVIGATOR_EFFORT)
    save.add_argument("--builder-model")
    save.add_argument("--builder-effort", default=DEFAULT_BUILDER_EFFORT)
    save.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    path = args.path
    try:
        if args.command == "defaults":
            payload: Any = recommended_defaults()
        elif args.command == "show":
            last = load_last_pairing(path)
            if last is None:
                payload = {**recommended_defaults(), "source": "defaults"}
            else:
                payload = {
                    **last,
                    "source": "last-pairing",
                    "reuse": format_reuse_line(last),
                }
        elif args.command == "prompt":
            last = load_last_pairing(path)
            prompt_text = format_preflight_prompt(last)
            if args.json:
                print(
                    json.dumps(
                        {"ok": True, "prompt": prompt_text, "has_last": last is not None},
                        ensure_ascii=False,
                    )
                )
                return 0
            print(prompt_text)
            return 0
        else:  # save
            validated = validate_pairing(
                {
                    "navigator_model": args.navigator_model,
                    "navigator_effort": args.navigator_effort,
                    "builder_model": args.builder_model,
                    "builder_effort": args.builder_effort,
                }
            )
            saved_path = save_last_pairing(validated, path)
            payload = {
                **validated,
                "path": str(saved_path),
                "reuse": format_reuse_line(validated),
            }
        if args.json:
            print(json.dumps({"ok": True, **payload}, ensure_ascii=False))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except PrefsError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
