#!/usr/bin/env python3
"""prewalk: lightweight model discovery and optional Builder diagnostics.

    prewalk.py models [--json] [--navigator-host HOST]     list usable models; never writes config
    prewalk.py doctor [--json] [--navigator-host HOST]     optional diagnosis; exit 3 = nothing ready
    prewalk.py setup  [--backend ID --model M --effort E --max-concurrency N] [--json]
    prewalk.py test   [--backend ID --model M --effort E] [--keep] [--timeout S]

models lists implemented, installed, ready backends and their model/effort choices.
setup writes ~/.prewalk/config.json (no secrets). test runs one Builder round trip
in a throwaway git repo through the async supervisor. None is a required first-run gate.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import builder_backends as bb  # noqa: E402
import prewalk_config  # noqa: E402

EXIT_NOTHING_READY = 3


def detect_navigator_host(explicit: str | None = None) -> tuple[str | None, str]:
    if explicit:
        return explicit, "--navigator-host"
    env = os.environ.get("PREWALK_HOST", "").strip()
    if env:
        return env, "PREWALK_HOST"
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude-code", "auto (CLAUDECODE)"
    if any(k.startswith("CODEX_") and k not in ("CODEX_API_KEY", "CODEX_HOME") for k in os.environ):
        return "codex", "auto (CODEX_* env)"
    if any(k.startswith("KIMI_") for k in os.environ):
        return "kimi", "auto (KIMI_* env)"
    return None, "unknown (pass --navigator-host)"


def available_models(navigator_host: str | None = None) -> dict[str, Any]:
    host, host_source = detect_navigator_host(navigator_host)
    backends = []
    for backend in bb.listed():
        row = backend.describe()
        ready = row["implemented"] and row["installed"] and row["auth"] == "ready"
        if row["track"] == "A" and row["billing"] != "subscription":
            ready = False
        if not ready:
            continue
        models = list(dict.fromkeys(m for m in [row["weak_model"], *row["alt_models"]] if m))
        backends.append({"backend": row["id"], "models": models,
                         "default_model": row["weak_model"], "default_effort": row["weak_effort"],
                         "efforts": row["efforts"]})
    return {"ok": True, "navigator_host": host, "navigator_host_source": host_source,
            "backends": backends}


def doctor(navigator_host: str | None = None) -> tuple[dict[str, Any], int]:
    host, host_source = detect_navigator_host(navigator_host)
    try:
        cfg, cfg_source = prewalk_config.load()
        cfg_error = None
    except prewalk_config.ConfigError as exc:
        cfg, cfg_source, cfg_error = {"builder": {}}, "invalid", str(exc)
    rows = [b.describe() for b in bb.listed()]
    saved = cfg.get("builder") or {}
    rec = bb.recommend(rows, host, saved if saved.get("backend") else None)
    notes: list[str] = []
    if cfg_error:
        notes.append(f"config ignored: {cfg_error}")
    if cfg_source == "migrated":
        notes.append(f"found legacy {cfg.get('migrated_from')}; `prewalk setup` will migrate it")
    if saved.get("backend") and (not rec or rec["reason"] != "saved config"):
        notes.append(f"saved backend {saved['backend']} is not ready; recommending another")
    if rec:
        b = bb.get(rec["backend"])
        fam = bb.family_of_host(host)
        if fam and b.family == fam:
            notes.append(f"{b.id} shares your {host} plan quota with the Navigator; "
                         f"the weak model ({rec['model']}) keeps the Builder cheap")
        elif host:
            notes.append(f"no ready {host} backend; using {b.id} (separate plan quota)")
        notes.append(f"blocked as Builder on {b.id}: {', '.join(b.blocked_models) or 'none'}")
        notes.append("Builder concurrency: 1 by default (config max 2)")
        row = next(r for r in rows if r["id"] == b.id)
        if row["billing"] not in ("subscription", "unknown") and b.track == "A":
            notes.append(f"{b.id} is logged in with {row['billing']}, not a subscription; "
                         "usage will be billed to that account")
    for r in rows:
        notes.extend(f"{r['id']}: {n}" for n in r["notes"])
    report = {"ok": rec is not None, "navigator_host": host, "navigator_host_source": host_source,
              "backends": rows, "recommended": rec, "config_path": str(prewalk_config.config_path()),
              "config_source": cfg_source, "notes": notes}
    if rec is None:
        report["fallback"] = bb.FALLBACK
    return report, (0 if rec else EXIT_NOTHING_READY)


def _print_doctor(report: dict[str, Any]) -> None:
    print(f"Navigator host: {report['navigator_host'] or '?'}  ({report['navigator_host_source']})")
    print(f"{'backend':16} {'track':6} {'installed':9} {'auth':14} {'billing':12} model")
    for r in report["backends"]:
        state = r["auth"] if r["implemented"] else "stub"
        print(f"{r['id']:16} {r['track']:6} {('yes' if r['installed'] else 'no'):9} {state:14} "
              f"{r['billing']:12} {r['weak_model'] or ''}")
    rec = report["recommended"]
    if rec:
        eff = rec["effort"] or "(not settable)"
        print(f"\nRecommended Builder: {rec['backend']}  model={rec['model']}  effort={eff}"
              f"  [{rec['reason']}]")
        print(f"Accept with: prewalk.py setup --backend {rec['backend']} --model {rec['model']}"
              + (f" --effort {rec['effort']}" if rec["effort"] else ""))
    else:
        print("\n" + report["fallback"])
    for n in report["notes"]:
        print(f"note: {n}")


def setup(backend: str | None, model: str | None, effort: str | None,
          max_concurrency: int | None, navigator_host: str | None) -> dict[str, Any]:
    try:
        cfg, source = prewalk_config.load()
    except prewalk_config.ConfigError:
        cfg, source = {"builder": {}}, "default"
    builder = dict(cfg.get("builder") or {})
    if backend is None and not builder.get("backend"):
        report, code = doctor(navigator_host)
        if code != 0:
            raise SystemExit(bb.FALLBACK)
        rec = report["recommended"]
        backend, model = rec["backend"], model or rec["model"]
        effort = effort or rec["effort"]
    if backend is not None:
        b = bb.get(backend)
        if not b.implemented:
            raise bb.BackendNotImplemented(f"{b.id} is a registry stub; pick an implemented backend")
        if backend != builder.get("backend"):
            builder.pop("model", None)
            builder.pop("effort", None)
        builder["backend"] = b.id
    b = bb.get(builder["backend"])
    builder["model"] = b.check_model(model or builder.get("model"))
    eff = effort or builder.get("effort") or b.weak_effort
    if eff:
        b.resolve_effort(eff, builder["model"])
        builder["effort"] = eff
    else:
        builder.pop("effort", None)
    if max_concurrency is not None:
        builder["max_concurrency"] = max_concurrency
    cfg["builder"] = builder
    host, _ = detect_navigator_host(navigator_host)
    if host:
        cfg["navigator_host"] = host
    cfg["updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    path = prewalk_config.save(cfg)
    clean, _ = prewalk_config.load(path)
    return {"ok": True, "config_path": str(path), "migrated": source == "migrated", "config": clean,
            "effective_effort": b.resolve_effort(clean["builder"].get("effort"), clean["builder"]["model"])}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "prewalk", "GIT_AUTHOR_EMAIL": "prewalk@localhost",
                        "GIT_COMMITTER_NAME": "prewalk", "GIT_COMMITTER_EMAIL": "prewalk@localhost"})


def selftest(backend: str | None, model: str | None, effort: str | None, keep: bool,
             timeout: float) -> tuple[dict[str, Any], int]:
    import prewalk_async
    import prewalk_layers
    if backend is None:
        cfg, _ = prewalk_config.load()
        backend = (cfg.get("builder") or {}).get("backend")
        model = model or (cfg.get("builder") or {}).get("model")
        effort = effort or (cfg.get("builder") or {}).get("effort")
    if not backend:
        return {"ok": False, "error": "no backend configured; run `prewalk.py doctor` then `setup`"}, 2
    b = bb.get(backend)
    tmp = Path(tempfile.mkdtemp(prefix="prewalk-test-"))
    repo, run = tmp / "repo", tmp / "run"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("prewalk self-test\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    prewalk_layers.snapshot(repo, run, "selftest", "L0", "navigator", "inherited baseline")
    (repo / "hello.txt").write_text("first edit by navigator\n", encoding="utf-8")
    brief = {"handoff_id": "selftest", "backend": b.id, "goal":
             "Append the line `hello from builder` to hello.txt. Change nothing else.",
             "success": ["hello.txt has a second line"], "scope": ["hello.txt"], "cwd": str(repo)}
    if model:
        brief["model"] = model
    if effort:
        brief["effort"] = effort
    (tmp / "brief.json").write_text(json.dumps(brief), encoding="utf-8")
    result: dict[str, Any] = {"backend": b.id, "workdir": str(tmp)}
    try:
        prewalk_async.launch(run, tmp / "brief.json")
        st = prewalk_async.wait(run, timeout)
        if not st["terminal"]:
            prewalk_async.cancel(run, timeout=10)
            st = prewalk_async.status(run)
        state = json.loads((run / "state.json").read_text(encoding="utf-8"))
        lines = (repo / "hello.txt").read_text(encoding="utf-8").splitlines()
        review_path = run / "review.json"
        review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
        changed_paths = {entry["path"] for entry in review.get("overall", [])}
        expected_second = "hello from mock builder turn 1" if b.id == "mock" else "hello from builder"
        checks = {"completed": st["status"] == "completed",
                  "file_changed": len(lines) >= 2,
                  "first_edit_preserved": lines[:1] == ["first edit by navigator"],
                  "builder_instruction_followed": lines == ["first edit by navigator", expected_second],
                  "scope_preserved": changed_paths == {"hello.txt"},
                  "review_md": (run / "review.md").exists(),
                  "model_ok": not state.get("model_warning")}
        result.update(status=st["status"], checks=checks, model=state.get("model"),
                      requested_effort=state.get("requested_effort"),
                      effective_effort=state.get("effective_effort"),
                      observed_models=state.get("observed_models"),
                      error=state.get("error") or state.get("limit_message"))
        result["ok"] = all(checks.values())
    except (prewalk_async.AsyncError, bb.BackendError, OSError) as exc:
        result.update(ok=False, error=str(exc))
    finally:
        if not keep and result.get("ok"):
            shutil.rmtree(tmp, ignore_errors=True)
            result["workdir"] = None
    return result, (0 if result.get("ok") else 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prewalk", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("models", help="read-only list of currently usable Builder models")
    p.add_argument("--json", action="store_true")
    p.add_argument("--navigator-host", choices=bb.NAVIGATOR_HOSTS)
    p = sub.add_parser("doctor", help="optional readiness diagnosis + recommendation")
    p.add_argument("--json", action="store_true")
    p.add_argument("--navigator-host", choices=bb.NAVIGATOR_HOSTS)
    p = sub.add_parser("setup", help="write ~/.prewalk/config.json (no secrets)")
    p.add_argument("--backend"); p.add_argument("--model")
    p.add_argument("--effort", choices=bb.CANONICAL_EFFORTS)
    p.add_argument("--max-concurrency", type=int, choices=(1, 2))
    p.add_argument("--navigator-host", choices=bb.NAVIGATOR_HOSTS)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("test", help="Builder round trip in a throwaway git repo")
    p.add_argument("--backend"); p.add_argument("--model")
    p.add_argument("--effort", choices=bb.CANONICAL_EFFORTS)
    p.add_argument("--keep", action="store_true"); p.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    try:
        if args.command == "models":
            report = available_models(args.navigator_host)
            if args.json:
                print(json.dumps(report, indent=2, ensure_ascii=False))
            else:
                for row in report["backends"]:
                    effort = row["default_effort"] or "(not settable)"
                    print(f"{row['backend']}: models={', '.join(row['models'])}; "
                          f"default={row['default_model']}/{effort}; "
                          f"efforts={', '.join(row['efforts']) or '(not settable)'}")
                if not report["backends"]:
                    print("No ready Builder models detected.")
            return 0
        if args.command == "doctor":
            report, code = doctor(args.navigator_host)
            if args.json:
                print(json.dumps(report, indent=2, ensure_ascii=False))
            else:
                _print_doctor(report)
            return code
        if args.command == "setup":
            result = setup(args.backend, args.model, args.effort, args.max_concurrency, args.navigator_host)
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                bcfg = result["config"]["builder"]
                print(f"saved {result['config_path']}: backend={bcfg['backend']} model={bcfg['model']} "
                      f"effort={bcfg.get('effort') or '-'} max_concurrency={bcfg['max_concurrency']}"
                      + ("  (migrated last-pairing.json)" if result["migrated"] else ""))
                print("next: prewalk.py test")
            return 0
        result, code = selftest(args.backend, args.model, args.effort, args.keep, args.timeout)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return code
    except (bb.BackendError, prewalk_config.ConfigError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
