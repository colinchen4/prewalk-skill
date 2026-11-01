#!/usr/bin/env python3
"""Per-run cost savings estimate for Prewalk.

For one run, estimates the percentage of cost saved versus the Navigator doing
all the work itself:  actual = navigator tokens @ navigator price + builder
tokens @ builder price;  baseline = navigator tokens @ navigator price +
builder tokens @ navigator price.  Prices come from a user-maintained
~/.prewalk/prices.json (or $PREWALK_PRICES_PATH); nothing is built in,
so a missing price yields an unpriced report of tokens only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import prewalk_config
import prewalk_plan
import prewalk_runtime

CATEGORY_MAP = {"inputOther": "input", "inputCacheRead": "cache_read",
                "inputCacheCreation": "cache_write", "output": "output"}
CATEGORIES = ("input", "cache_read", "cache_write", "output")
ASSUMPTIONS = [
    "Counterfactual assumes the Navigator would have used the same token counts for the delegated work.",
    "No difference in thinking effort or rework between Builder and Navigator is modelled.",
    "Prices come from prices.json as of its modification time; this is a client-side estimate, not an invoice.",
]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


def _iso_to_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp() * 1000)


def prices_path() -> Path:
    env = os.environ.get("PREWALK_PRICES_PATH", "").strip()
    return Path(env).expanduser() if env else prewalk_config.home_dir() / "prices.json"


def history_path() -> Path:
    return prewalk_config.home_dir() / "savings-history.jsonl"


def load_prices(path: Path | None = None) -> dict[str, Any]:
    path = path or prices_path()
    raw = json.loads(prewalk_runtime._read_text_with_retry(path))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: prices file must be a JSON object")
    return raw


def resolve_price(prices: dict[str, Any], model: str | None) -> dict[str, Any] | None:
    if not model:
        return None
    aliases = prices.get("aliases") if isinstance(prices.get("aliases"), dict) else {}
    seen = set()
    key = model
    while key in aliases and key not in seen:
        seen.add(key)
        key = aliases[key]
    entry = prices.get(key)
    return entry if isinstance(entry, dict) else None


def tokens_from_totals(totals: Any) -> dict[str, float]:
    totals = totals if isinstance(totals, dict) else {}
    out = {}
    for raw_key, category in CATEGORY_MAP.items():
        value = totals.get(raw_key, totals.get(category, 0))
        out[category] = float(value) if isinstance(value, (int, float)) else 0.0
    return out


def cost(tokens: dict[str, float], price: dict[str, Any]) -> float:
    total = 0.0
    for category in CATEGORIES:
        rate = price.get(category)
        if not isinstance(rate, (int, float)):
            raise ValueError(f"price for {category} is missing")
        total += tokens.get(category, 0.0) * rate
    return total / 1_000_000.0


def _section_by_model(section: dict[str, Any]) -> dict[str | None, dict[str, float]]:
    """Aggregate usage tokens per model. Prefer a per-call list (`calls` with
    `model` + `usage`); otherwise fall back to `totals` under the first model."""
    calls = section.get("calls")
    if isinstance(calls, list):
        by_model: dict[str | None, dict[str, float]] = {}
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("usage"), dict):
                continue
            model = call.get("model") if isinstance(call.get("model"), str) else None
            bucket = by_model.setdefault(model, {})
            for raw_key in CATEGORY_MAP:
                value = call["usage"].get(raw_key)
                if isinstance(value, (int, float)):
                    bucket[raw_key] = bucket.get(raw_key, 0.0) + value
        if by_model:
            return {model: tokens_from_totals(t) for model, t in by_model.items()}
    models = section.get("models")
    model = (next((m for m in models if isinstance(m, str) and m), None)
             if isinstance(models, list) else None)
    return {model: tokens_from_totals(section.get("totals"))}


def _sum_tokens(by_model: dict[str | None, dict[str, float]]) -> dict[str, float]:
    total = {category: 0.0 for category in CATEGORIES}
    for tokens in by_model.values():
        for category in CATEGORIES:
            total[category] += tokens.get(category, 0.0)
    return total


def _primary_model(by_model: dict[str | None, dict[str, float]]) -> str | None:
    best = None
    best_tokens = -1.0
    for model, tokens in by_model.items():
        total = sum(tokens.values())
        if model is not None and total > best_tokens:
            best, best_tokens = model, total
    return best


def _price_by_model(by_model: dict[str | None, dict[str, float]],
                    prices: dict[str, Any]) -> tuple[float, list[str]]:
    """Price each model's tokens at its own price. Returns (usd, unpriced models)."""
    total = 0.0
    missing = []
    for model, tokens in by_model.items():
        price = resolve_price(prices, model)
        try:
            total += cost(tokens, price) if price is not None else 0.0
        except ValueError:
            price = None
        if price is None:
            missing.append(model or "unknown")
    return total, missing


def _builder_usage(run_dir: Path) -> tuple[dict[str | None, dict[str, float]], str | None]:
    usage_path = run_dir / "builder-usage.json"
    if not usage_path.is_file():
        usage_path = run_dir / "usage.json"
    try:
        data = json.loads(prewalk_runtime._read_text_with_retry(usage_path))
    except (OSError, ValueError):
        return {}, None
    builder = data.get("builder", data) if isinstance(data, dict) else {}
    if not isinstance(builder, dict):
        builder = {}
    by_model = _section_by_model(builder)
    return by_model, _primary_model(by_model)


def _run_json_model(run_dir: Path) -> str | None:
    try:
        data = json.loads(prewalk_runtime._read_text_with_retry(run_dir / "run.json"))
    except (OSError, ValueError):
        return None
    model = data.get("navigator_model") if isinstance(data, dict) else None
    return model if isinstance(model, str) and model else None


def _recorded_navigator(run_dir: Path) -> dict[str, Any] | None:
    usage_path = run_dir / "usage.json"
    try:
        data = json.loads(prewalk_runtime._read_text_with_retry(usage_path))
    except (OSError, ValueError):
        return None
    section = data.get("navigator")
    if not isinstance(section, dict):
        return None
    models = section.get("models")
    model = (next((m for m in models if isinstance(m, str) and m), None)
             if isinstance(models, list) else None)
    if isinstance(section.get("totals"), dict) or isinstance(section.get("calls"), list):
        by_model = _section_by_model(section)
        return {"tokens": _sum_tokens(by_model), "by_model": by_model,
                "model": _primary_model(by_model) or model, "status": "recorded"}
    if model:
        return {"tokens": None, "by_model": None, "model": model,
                "status": section.get("usage_status") or "not_collected"}
    return None


def _kimi_session_list(cwd: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    kimi = prewalk_runtime._find_kimi_binary()
    if not kimi:
        return None, "kimi CLI not found; set KIMI_BIN"
    try:
        proc = subprocess.run(
            prewalk_runtime._kimi_argv(kimi, ["session", "list", "--cwd", str(cwd), "--json"]),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"cannot run kimi session list: {exc}"
    if proc.returncode != 0:
        return None, f"kimi session list exited {proc.returncode}"
    try:
        sessions = json.loads(proc.stdout.lstrip("﻿"))
    except json.JSONDecodeError as exc:
        return None, f"kimi session list returned invalid JSON: {exc.msg}"
    if isinstance(sessions, dict):
        sessions = sessions.get("sessions", [])
    if not isinstance(sessions, list):
        return None, "kimi session list returned an unexpected shape"
    return [s for s in sessions if isinstance(s, dict)], None


def _kimi_navigator_usage(run_dir: Path, state: dict[str, Any], plan: dict[str, Any],
                          start_ms: int | None, end_ms: int | None) -> dict[str, Any]:
    sessions, error = _kimi_session_list(Path(plan["cwd"]))
    if sessions is None:
        return {"tokens": None, "model": None, "status": "unavailable", "reason": error}
    run_dir_str = str(run_dir)
    run_dir_posix = Path(run_dir).as_posix()
    markers = {run_dir_str, run_dir_posix,
               json.dumps(run_dir_str)[1:-1], json.dumps(run_dir_posix)[1:-1]}
    matches = []
    for session in sessions:
        if session.get("id") == state.get("session_id"):
            continue
        session_dir = session.get("sessionDir")
        if not isinstance(session_dir, str) or not session_dir:
            continue
        wire = Path(session_dir) / "agents" / "main" / "wire.jsonl"
        try:
            text = wire.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if any(marker in text for marker in markers):
            matches.append((session, wire))
    if not matches:
        return {"tokens": None, "model": None, "status": "unavailable",
                "reason": "no Navigator session wire references this run dir"}
    if len(matches) > 1:
        return {"tokens": None, "model": None, "status": "unavailable",
                "reason": f"{len(matches)} Navigator sessions reference this run dir"}
    _, wire = matches[0]
    segments, _ = prewalk_runtime._read_kimi_wire(wire)
    records = [record for segment in segments for record in segment]
    skipped_scope = sum(1 for r in records
                        if r.get("usageScope") not in (None, "turn"))
    records = [r for r in records if r.get("usageScope") in (None, "turn")]
    if start_ms is not None:
        records = [r for r in records if not isinstance(r.get("time"), (int, float))
                   or r["time"] >= start_ms]
    if end_ms is not None:
        records = [r for r in records if not isinstance(r.get("time"), (int, float))
                   or r["time"] <= end_ms]
    calls = [{"model": r.get("model"), "usage": r.get("usage")}
             for r in records if isinstance(r.get("usage"), dict)]
    by_model = _section_by_model({"calls": calls})
    return {"tokens": _sum_tokens(by_model) if by_model else tokens_from_totals({}),
            "by_model": by_model, "model": _primary_model(by_model),
            "status": "auto", "reason": None, "calls": len(calls),
            "skipped_scope": skipped_scope}


def _navigator_overhead(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "navigator-calls.jsonl"
    count = 0
    chars = 0
    if path.exists():
        for line in prewalk_runtime._read_text_with_retry(path).splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            count += 1
            if isinstance(record.get("stdout_chars"), (int, float)):
                chars += record["stdout_chars"]
    return {"navigator_cli_calls": count, "navigator_stdout_chars": chars}


def build_report(run_dir: Path, state: dict[str, Any] | None = None,
                 plan: dict[str, Any] | None = None) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    if state is None:
        state = json.loads(prewalk_runtime._read_text_with_retry(run_dir / "state.json"))
    if plan is None:
        plan = prewalk_plan.load_plan(Path(state["plan"]))
    errors: list[str] = []
    start_ms = _iso_to_ms(plan.get("created_at") or state.get("created_at"))
    end_ms = _iso_to_ms(state.get("ended_at"))
    window = {"start": plan.get("created_at") or state.get("created_at"),
              "end": state.get("ended_at"), "start_ms": start_ms, "end_ms": end_ms}

    builder_by_model, builder_model = _builder_usage(run_dir)
    builder_tokens = _sum_tokens(builder_by_model)
    navigator = _recorded_navigator(run_dir)
    if navigator is None and state.get("backend") == "kimi-sub":
        navigator = _kimi_navigator_usage(run_dir, state, plan, start_ms, end_ms)
    if navigator is None:
        navigator = {"tokens": None, "by_model": None, "model": None,
                     "status": "not_collected", "reason": None}
    if navigator["status"] in ("unavailable", "not_collected") and navigator.get("reason"):
        errors.append(f"navigator usage {navigator['status']}: {navigator['reason']}")

    # Navigator model, in priority order: recorded usage section -> auto Kimi
    # wire records -> run.json navigator_model (legacy runs) -> unknown.
    navigator_model = navigator.get("model") or _run_json_model(run_dir)
    if navigator_model is None:
        errors.append("navigator model unknown; "
                      "run record-usage --model <navigator model>")

    price_file = prices_path()
    prices: dict[str, Any] = {}
    prices_mtime = None
    try:
        prices = load_prices(price_file)
        prices_mtime = _dt.datetime.fromtimestamp(
            price_file.stat().st_mtime, _dt.timezone.utc).isoformat(timespec="seconds")
    except FileNotFoundError:
        errors.append(f"prices file not found: {price_file}")
    except (OSError, ValueError) as exc:
        errors.append(f"cannot read prices: {exc}")

    builder_price = resolve_price(prices, builder_model)
    navigator_price = resolve_price(prices, navigator_model)
    builder_cost, unpriced_builder = _price_by_model(builder_by_model, prices)
    nav_by_model = navigator.get("by_model") or {}
    navigator_cost, unpriced_nav = (_price_by_model(nav_by_model, prices)
                                    if nav_by_model else (0.0, []))
    unpriced_models = unpriced_builder + unpriced_nav
    for model in unpriced_models:
        errors.append(f"no price for model {model}")

    report: dict[str, Any] = {
        "schema_version": 1, "handoff_id": state.get("handoff_id"),
        "generated_at": _now(), "run_dir": str(run_dir), "window": window,
        "currency": "USD",
        "models": {"navigator": navigator_model, "builder": builder_model},
        "tokens": {"navigator": navigator.get("tokens"), "builder": builder_tokens,
                   "by_model": {"builder": builder_by_model,
                                "navigator": nav_by_model}},
        "skipped_scope_count": navigator.get("skipped_scope", 0),
        "prices": {"navigator": navigator_price, "builder": builder_price,
                   "path": str(price_file), "mtime": prices_mtime},
        "navigator_usage": navigator["status"],
        "navigator_usage_reason": navigator.get("reason"),
        "navigator_calls": navigator.get("calls"),
        "overhead": {**_navigator_overhead(run_dir),
                     "navigator_tokens": navigator.get("tokens")},
        "assumptions": ASSUMPTIONS, "errors": errors,
    }

    nav_usable = navigator["status"] in ("recorded", "auto") and navigator.get("tokens") is not None
    if navigator_model is None or navigator_price is None or unpriced_models:
        report["mode"] = "unpriced"
        report["cost_status"] = "unpriced"
        return report
    try:
        builder_at_nav = sum(cost(tokens, navigator_price)
                             for tokens in builder_by_model.values())
    except ValueError as exc:
        errors.append(str(exc))
        report["mode"] = "unpriced"
        report["cost_status"] = "unpriced"
        return report

    if nav_usable:
        actual = navigator_cost + builder_cost
        baseline = navigator_cost + builder_at_nav
        saved = baseline - actual
        fraction = 1 - actual / baseline if baseline else 0.0
        report.update(mode="full", cost_status="priced", actual=actual, baseline=baseline,
                      saved=saved, savings_fraction=fraction,
                      savings_pct=round(fraction * 100, 1))
        return report

    fraction = 1 - builder_cost / builder_at_nav if builder_at_nav else 0.0
    report.update(mode="builder_only", cost_status="priced", builder_cost=builder_cost,
                  builder_at_nav_price=builder_at_nav,
                  builder_only_savings_pct=round(fraction * 100, 1),
                  builder_only_fraction=fraction,
                  label="upper bound; excludes Navigator orchestration cost")
    return report


def savings_markdown(report: dict[str, Any]) -> str:
    mode = report.get("mode")
    if mode == "full":
        return (f"Savings: {report['savings_pct']}% of the all-Navigator baseline "
                f"(saved ${report['saved']:.4f} of ${report['baseline']:.4f}; mode=full).")
    if mode == "builder_only":
        return (f"Savings (upper bound, excludes Navigator orchestration): "
                f"{report['builder_only_savings_pct']}% on Builder work (mode=builder_only).")
    if mode == "error":
        return f"Savings report failed: {report.get('error')}"
    return f"Savings unpriced: tokens recorded, no prices available (mode={mode})."


def write_report(run_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(run_dir)
    prewalk_runtime._atomic_write_json(run_dir / "savings.json", report)
    (run_dir / "savings.md").write_text(savings_markdown(report) + "\n", encoding="utf-8")
    return report


def append_history(report: dict[str, Any], path: Path | None = None) -> None:
    """Append one entry per (handoff_id, run_dir); re-reporting replaces the
    existing entry for the same run instead of duplicating it."""
    path = path or history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    run_dir = report.get("run_dir")
    handoff = report.get("handoff_id")
    kept = []
    if path.exists():
        for line in (prewalk_runtime._read_text_with_retry(path) or "").splitlines():
            if not line.strip():
                continue
            try:
                old = json.loads(line)
            except json.JSONDecodeError:
                old = None
            same_key = isinstance(old, dict) and old.get("handoff_id") == handoff
            same_key = same_key and old.get("run_dir") == run_dir
            if same_key:
                continue
            kept.append(line)
    entry = {"handoff_id": handoff, "run_dir": run_dir,
             "date": report.get("generated_at"),
             "models": report.get("models") or {}, "mode": report.get("mode"),
             "pct": report.get("savings_pct", report.get("builder_only_savings_pct")),
             "saved": report.get("saved")}
    kept.append(json.dumps(entry, ensure_ascii=False))
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(chr(10).join(kept) + chr(10), encoding="utf-8")
    os.replace(tmp, path)


def run_report(run_dir: Path, state: dict[str, Any] | None = None,
               plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build, write and record the report; a failure is stored, never raised."""
    run_dir = Path(run_dir)
    try:
        report = build_report(run_dir, state, plan)
    except Exception as exc:  # report errors must never change run status
        report = {"schema_version": 1, "ok": False, "mode": "error", "error": str(exc),
                  "generated_at": _now()}
    try:
        write_report(run_dir, report)
        append_history(report)
    except OSError:
        pass
    return report


def summary_block(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not report:
        return None
    mode = report.get("mode")
    if mode == "full":
        return {"mode": "full", "savings_pct": report.get("savings_pct"),
                "saved": report.get("saved"), "baseline": report.get("baseline"),
                "currency": report.get("currency", "USD")}
    if mode == "builder_only":
        return {"mode": "builder_only", "savings_pct": report.get("builder_only_savings_pct"),
                "label": report.get("label"), "currency": report.get("currency", "USD")}
    return {"mode": mode or "unpriced"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    report_p = sub.add_parser("report")
    report_p.add_argument("--run-dir", required=True, type=Path)
    template = sub.add_parser("prices-template")
    template.add_argument("--models", nargs="+", required=True)
    history = sub.add_parser("history")
    history.add_argument("--last", type=int, default=20)
    args = parser.parse_args(argv)
    if args.command == "prices-template":
        skeleton = {model: {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
                    for model in args.models}
        skeleton["aliases"] = {}
        print(json.dumps(skeleton, indent=2, ensure_ascii=False))
        return 0
    if args.command == "history":
        path = history_path()
        lines = []
        if path.exists():
            lines = [json.loads(line) for line in
                     prewalk_runtime._read_text_with_retry(path).splitlines() if line.strip()]
        for entry in lines[-args.last:]:
            models = entry.get("models") or {}
            print(f"{entry.get('date', '')}  {entry.get('handoff_id', '')}  "
                  f"nav={models.get('navigator')}  builder={models.get('builder')}  "
                  f"mode={entry.get('mode')}  pct={entry.get('pct')}  saved={entry.get('saved')}")
        print(f"{len(lines)} recorded run(s)")
        return 0
    report = run_report(args.run_dir)
    print(json.dumps(summary_block(report) or {"ok": True, "mode": report.get("mode")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
