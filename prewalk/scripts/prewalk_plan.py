#!/usr/bin/env python3
"""Create and validate the Navigator-only plan required before Builder launch."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import prewalk_runtime

MODES = ("brief_only", "sample_guided", "navigator_only")
MAX_CONTEXT_CHARS = 1500
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")


def validate_plan(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("plan must be a JSON object")
    if raw.get("schema_version") != 1:
        raise ValueError("plan schema_version must be 1")
    if not ID_RE.match(raw.get("handoff_id", "")):
        raise ValueError("invalid handoff_id")
    if raw.get("mode") not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if raw.get("status") != "planned":
        raise ValueError("plan status must be 'planned' before Builder launch")
    if not isinstance(raw.get("goal"), str) or not raw["goal"].strip():
        raise ValueError("goal is required")
    for key in ("success", "scope"):
        value = raw.get(key)
        if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
            raise ValueError(f"{key} must be a non-empty list of strings")
    context = raw.get("context", "")
    if not isinstance(context, str) or len(context) > MAX_CONTEXT_CHARS:
        raise ValueError(f"context must be at most {MAX_CONTEXT_CHARS} characters")
    if not isinstance(raw.get("cwd"), str) or not raw["cwd"].strip():
        raise ValueError("cwd is required")
    validation = raw.get("validation", [])
    if not isinstance(validation, list) or not all(isinstance(v, str) and v.strip() for v in validation):
        raise ValueError("validation must be a list of command strings")
    return {**raw, "builder_required": raw["mode"] != "navigator_only"}


def create_plan(handoff_id: str, mode: str, goal: str, success: list[str], scope: list[str],
                context: str = "", cwd: str | None = None,
                validation: list[str] | None = None,
                model: str | None = None, effort: str | None = None) -> dict[str, Any]:
    return validate_plan({"schema_version": 1, "status": "planned", "handoff_id": handoff_id,
                          "mode": mode, "goal": goal, "success": success,
                          "scope": scope, "context": context, "cwd": cwd or str(Path.cwd()),
                          "validation": validation or [], "model": model, "effort": effort})


def write_plan(run_dir: Path, plan: dict[str, Any]) -> Path:
    path = run_dir / "plan.json"
    prewalk_runtime._atomic_write_json(path, validate_plan(plan))
    return path


def load_plan(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(prewalk_runtime._read_text_with_retry(path))
    except FileNotFoundError as exc:
        raise ValueError(f"plan not found: {path}") from exc
    return validate_plan(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--run-dir", required=True, type=Path)
    create.add_argument("--handoff-id", required=True)
    create.add_argument("--mode", required=True, choices=MODES)
    create.add_argument("--goal", required=True)
    create.add_argument("--success", action="append", required=True)
    create.add_argument("--scope", action="append", required=True)
    create.add_argument("--context", default="")
    create.add_argument("--cwd", default=str(Path.cwd()))
    create.add_argument("--validation", action="append", default=[])
    create.add_argument("--model")
    create.add_argument("--effort")
    validate = sub.add_parser("validate")
    validate.add_argument("--plan", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            plan = create_plan(args.handoff_id, args.mode, args.goal, args.success, args.scope,
                               context=args.context, cwd=args.cwd, validation=args.validation,
                               model=args.model, effort=args.effort)
            path = write_plan(args.run_dir, plan)
            result = {"ok": True, "plan": str(path), "builder_required": plan["builder_required"]}
        else:
            plan = load_plan(args.plan)
            result = {"ok": True, "mode": plan["mode"], "builder_required": plan["builder_required"]}
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
