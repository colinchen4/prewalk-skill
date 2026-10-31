#!/usr/bin/env python3
"""Persistent, isolated Builder execution with evidence-only Navigator approval."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import persistent_backends
import prewalk_async
import prewalk_hash
import prewalk_layers
import prewalk_plan
import prewalk_runtime

TERMINAL = {"approved", "discarded", "failed", "cancelled", "merge_conflict"}
WAITING = {"AWAITING_NAVIGATOR", "READY_FOR_REVIEW", "VALIDATION_FAILED"}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


def _state_path(run_dir: Path) -> Path:
    return run_dir / "state.json"


def _read_state(run_dir: Path) -> dict[str, Any]:
    return json.loads(prewalk_runtime._read_text_with_retry(_state_path(run_dir)))


def _write_state(run_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    prewalk_runtime._atomic_write_json(_state_path(run_dir), state)


def _commands_path(run_dir: Path) -> Path:
    return run_dir / "commands.jsonl"


def _event(run_dir: Path, event: dict[str, Any]) -> None:
    prewalk_async.append_event(run_dir / "progress.jsonl", event)


def phase_prompt(plan: dict[str, Any], run_dir: Path, phase: str, delta: str = "") -> str:
    lines = ["PREWALK_PHASE: PERSISTENT_BUILDER", f"HANDOFF_ID: {plan['handoff_id']}",
             f"SESSION_CACHE_KEY: prewalk/{plan['handoff_id']}", "",
             f"GOAL: {plan['goal']}", "SUCCESS:", *[f"- {item}" for item in plan["success"]],
             "FILE SCOPE:", *[f"- {item}" for item in plan["scope"]]]
    if plan.get("context"):
        lines += ["CONTEXT:", plan["context"]]
    index = run_dir / "exploration-index.json"
    if index.exists():
        lines.append(f"EXECUTION INDEX: {index}; query only what you need")
    if (run_dir / "first-edit.patch").exists():
        lines.append(f"FIRST_EDIT_PATCH: {run_dir / 'first-edit.patch'}; read the complete patch")
    lines.append(f"CURRENT_PHASE: {phase}")
    if phase == "EXPLORING":
        lines += ["Do not edit any file. Check only definitions, references, and tests inside scope.",
                  "Return EXPLORATION_READY and any concrete contradiction, then wait for EXECUTE.",
                  "Do not return NAVIGATOR_REVIEW and do not end this session."]
    else:
        lines += ["Implement the approved plan now. Read the complete L1 patch when present.",
                  "Do not change the plan or scope. The supervisor independently runs validation.",
                  "Return only after implementation and relevant checks. Prose alone is not evidence."]
    if delta:
        lines += ["NEW DELTA:", delta]
    return "\n".join(lines) + "\n"


def launch(run_dir: Path, plan_path: Path, backend_id: str) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    plan = prewalk_plan.load_plan(plan_path)
    if not plan["builder_required"]:
        raise ValueError("navigator_only plan must not launch Builder")
    if not plan["validation"] and not plan.get("allow_unverified"):
        raise ValueError("validation commands are required for independent acceptance")
    source = Path(plan["cwd"]).resolve()
    root = prewalk_layers.repo_root(source)
    if root is None:
        raise ValueError("plan cwd must be a Git worktree")
    try:
        run_dir.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("run_dir must be outside the source worktree")
    if (run_dir / "worktree").exists():
        raise ValueError("Builder worktree already exists")
    run_dir.mkdir(parents=True, exist_ok=True)
    doc = prewalk_layers.load(run_dir)
    if doc is None:
        prewalk_layers.snapshot(source, run_dir, plan["handoff_id"], "L0", "navigator", "inherited baseline")
        doc = prewalk_layers.load(run_dir)
    base_layer = "L1" if plan["mode"] == "sample_guided" else "L0"
    if base_layer == "L1":
        layer = prewalk_layers._layer(doc, "L1")
        if not layer:
            raise ValueError("sample_guided requires Navigator's L1 before Builder launch")
        base_sha = layer["sha"]
    else:
        base_sha = doc["base"]["sha"]
    builder_wt = run_dir / "worktree"
    with prewalk_async._active_lock():
        prewalk_async._check_concurrency(run_dir)
        prewalk_layers.git(root, "worktree", "add", "--detach", str(builder_wt), base_sha)
        state = {"schema_version": 1, "handoff_id": plan["handoff_id"], "status": "starting",
                 "phase": "PLANNED", "mode": plan["mode"], "backend": backend_id,
                 "model": plan.get("model"), "effort": plan.get("effort"),
                 "source_worktree": str(source), "builder_worktree": str(builder_wt),
                 "base_layer": base_layer, "base_sha": base_sha, "plan": str(plan_path),
                 "cache_key": f"prewalk/{plan['handoff_id']}",
                 "created_at": _now(), "cold_starts": 1}
        _write_state(run_dir, state)
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_supervise",
                                 "--run-dir", str(run_dir)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, close_fds=True,
                                **persistent_backends.base.popen_group_kwargs())
        state["supervisor_pid"] = proc.pid
        _write_state(run_dir, state)
        prewalk_runtime._atomic_write_json(prewalk_async._active_file(run_dir),
                                           {"run_dir": str(run_dir), "supervisor_pid": proc.pid,
                                            "backend": backend_id, "since": _now()})
    return {"ok": True, "run_dir": str(run_dir), "supervisor_pid": proc.pid,
            "builder_worktree": str(builder_wt), "next": "status, then execute after AWAITING_NAVIGATOR"}


def run_validation(run_dir: Path, plan: dict[str, Any]) -> list[dict[str, Any]]:
    worktree = run_dir / "worktree"
    output = run_dir / "validation"
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for index, command in enumerate(plan["validation"], 1):
        started = _now()
        stdout_path = output / f"{index:03d}.out"
        stderr_path = output / f"{index:03d}.err"
        try:
            proc = subprocess.run(command, shell=True, cwd=worktree, capture_output=True, timeout=600)
            code = proc.returncode
            stdout_path.write_bytes(proc.stdout)
            stderr_path.write_bytes(proc.stderr)
        except subprocess.TimeoutExpired as exc:
            code = 124
            stdout_path.write_bytes(exc.stdout or b"")
            stderr_path.write_bytes((exc.stderr or b"") + b"\nvalidation timed out\n")
        record = {"command": command, "cwd": str(worktree), "exit_code": code,
                  "started_at": started, "ended_at": _now(),
                  "stdout": str(stdout_path), "stderr": str(stderr_path)}
        prewalk_runtime._atomic_write_json(output / f"{index:03d}.json", record)
        records.append(record)
    return records


def build_review(run_dir: Path) -> dict[str, Any]:
    state = _read_state(run_dir)
    doc = prewalk_layers.load(run_dir)
    if not doc or not doc["layers"]:
        raise ValueError("no Builder layer is available for review")
    final = doc["layers"][-1]
    base = state["base_sha"]
    entries = prewalk_layers.manifest(Path(state["builder_worktree"]), base, final["sha"])
    patch = prewalk_layers.git(Path(state["builder_worktree"]), "diff", "--no-renames", base, final["sha"])
    patch_path = run_dir / "builder-review.patch"
    patch_unchanged = prewalk_hash.write_text_if_changed(patch_path, patch)
    validations = []
    for path in sorted((run_dir / "validation").glob("*.json")):
        validations.append(json.loads(path.read_text(encoding="utf-8")))
    report = {"ok": True, "base": base, "final": final["sha"], "changed": entries,
              "validation": validations, "patch": str(patch_path),
              "patch_sha256": prewalk_hash.sha256_text(patch), "patch_unchanged": patch_unchanged,
              "has_diff": bool(entries), "validation_passed": bool(validations) and all(
                  record["exit_code"] == 0 for record in validations)}
    prewalk_runtime._atomic_write_json(run_dir / "builder-review.json", report)
    return report


def record_config_evidence(state: dict[str, Any], backend: persistent_backends.JsonRpcBackend) -> None:
    evidence = backend.current_config()
    state["config_evidence"] = evidence
    actual = evidence.get("model")
    state["observed_model"] = actual
    requested = state.get("model")
    if requested and actual == requested:
        state["model_status"] = "verified"
        state.pop("model_warning", None)
    elif requested and actual and actual != requested:
        state["model_status"] = "warning"
        state["model_warning"] = f"requested {requested}, ACP reports {actual}"
    else:
        state["model_status"] = "unverified"


def collect_usage(run_dir: Path, state: dict[str, Any], turn: int) -> None:
    if state["backend"] != "kimi-sub":
        state["usage_status"] = "unavailable"
        return
    session = state["session_id"]
    events = run_dir / "builder-events.jsonl"
    direct_path = run_dir / "builder-usage.json"
    try:
        prior = prewalk_runtime._load_prior_usage(direct_path, "kimi", session, turn) if turn > 1 else None
        usage = prewalk_runtime._kimi_usage(session, Path(state["builder_worktree"]), turn, prior, events)
    except (prewalk_runtime.InspectError, OSError, ValueError) as exc:
        usage = {"usage_status": "unavailable", "usage_error": str(exc), "models": []}
    prewalk_runtime._atomic_write_json(direct_path, usage)
    prewalk_runtime._atomic_write_json(run_dir / "usage.json", {
        "builder": usage, "navigator": {"usage_status": "not_collected"}})
    state["usage_status"] = usage["usage_status"]
    state["cache_usage"] = usage.get("totals")
    state["observed_models"] = usage.get("models") or []
    requested = state.get("model")
    if requested and state["observed_models"] and requested not in state["observed_models"]:
        state["model_status"] = "warning"
        state["model_warning"] = f"requested {requested}, usage reports {state['observed_models']}"


def apply_review(run_dir: Path) -> None:
    state = _read_state(run_dir)
    report = build_review(run_dir)
    if not report["has_diff"]:
        raise ValueError("Builder produced no diff; refusing approval")
    if not report["validation_passed"]:
        raise ValueError("validation is missing or failed; refusing approval")
    source = Path(state["source_worktree"])
    prewalk_layers.git(source, "apply", "--check", str(run_dir / "builder-review.patch"))
    prewalk_layers.git(source, "apply", str(run_dir / "builder-review.patch"))


def write_continuation(run_dir: Path, command: dict[str, Any]) -> Path:
    state = _read_state(run_dir)
    plan = prewalk_plan.load_plan(Path(state["plan"]))
    failures = []
    for path in sorted((run_dir / "validation").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["exit_code"] != 0:
            failures.append({"command": record["command"], "exit_code": record["exit_code"],
                             "stderr": record["stderr"]})
    continuation = {"schema_version": 1, "compaction_id": command["compaction_id"],
                    "handoff_id": state["handoff_id"], "goal": plan["goal"],
                    "success": plan["success"], "scope": plan["scope"], "context": plan.get("context", ""),
                    "base_layer": state["base_layer"], "base_sha": state["base_sha"],
                    "builder_worktree": state["builder_worktree"], "status": state["status"],
                    "failed_validation": failures}
    path = run_dir / f"continuation-{command['compaction_id']}.json"
    prewalk_hash.write_json_if_changed(path, continuation)
    return path


def queue(run_dir: Path, command: str, text: str = "", model: str | None = None,
          effort: str | None = None, reason: str = "", compaction_id: str = "") -> dict[str, Any]:
    state = _read_state(run_dir)
    if command == "execute" and state["status"] not in WAITING:
        raise ValueError(f"cannot execute while Builder is {state['status']}")
    if command == "approve" and state["status"] != "READY_FOR_REVIEW":
        raise ValueError("Builder is not READY_FOR_REVIEW")
    if command == "route":
        if state["status"] not in WAITING:
            raise ValueError("model routing requires an idle Builder state")
        if not model and not effort:
            raise ValueError("route requires a model or effort change")
        if not prewalk_plan.ID_RE.match(compaction_id):
            raise ValueError("route requires a valid compaction_id")
    if command == "discard" and state["status"] in TERMINAL:
        if state["status"] == "discarded":
            return {"ok": True, "status": "discarded"}
        worktree = run_dir / "worktree"
        if worktree.exists():
            subprocess.run(["git", "-C", state["source_worktree"], "worktree", "remove", "--force",
                            str(worktree)], capture_output=True, check=True)
        state["status"] = state["phase"] = "discarded"
        _write_state(run_dir, state)
        prewalk_async._release_active(run_dir)
        return {"ok": True, "status": "discarded"}
    if command in {"approve", "cancel"} and state["status"] in TERMINAL:
        return {"ok": True, "status": state["status"]}
    event = prewalk_async.append_event(_commands_path(run_dir),
                                       {"type": command, "text": text, "handoff_id": state["handoff_id"],
                                        "model": model, "effort": effort, "reason": reason,
                                        "compaction_id": compaction_id})
    return {"ok": True, "command_seq": event["seq"], "status": state["status"]}


def supervise(run_dir: Path) -> int:
    state = _read_state(run_dir)
    plan = prewalk_plan.load_plan(Path(state["plan"]))
    backend = None
    final_status = "failed"
    try:
        backend = persistent_backends.create_backend(state["backend"], run_dir / "worktree", run_dir,
                                                      state.get("model"), state.get("effort"))
        session = backend.start(state.get("model"), state.get("effort"))
        record_config_evidence(state, backend)
        state["cli_integrity"] = backend.integrity
        state["integrity_status"] = "verified"
        state.update(status="EXPLORING", phase="EXPLORING", supervisor_pid=os.getpid(),
                     builder_pid=backend.pid, session_id=session, started_at=_now())
        _write_state(run_dir, state)
        _event(run_dir, {"state": "EXPLORING", "builder_pid": backend.pid, "session_id": session})
        explore_prompt = phase_prompt(plan, run_dir, "EXPLORING")
        state["explore_brief_chars"] = len(explore_prompt)
        state["usage_status"] = "unavailable"
        prewalk_async.append_event(run_dir / "routes.jsonl",
                                   {"action": "launch", "to_model": state.get("model"),
                                    "to_effort": state.get("effort"), "reason": "planned backend"})
        backend.prompt(explore_prompt)
        collect_usage(run_dir, state, 1)
        prewalk_layers.snapshot(run_dir / "worktree", run_dir, state["handoff_id"], None,
                                "builder", "scoped exploration", skip_if_unchanged=True)
        state.update(status="AWAITING_NAVIGATOR", phase="AWAITING_NAVIGATOR",
                     exploration_ready_at=_now())
        _write_state(run_dir, state)
        seen = len(prewalk_async.read_tail(_commands_path(run_dir)))
        while True:
            if backend.proc.poll() is not None:
                raise persistent_backends.PersistentError("Builder process exited before review disposal")
            commands = prewalk_async.read_tail(_commands_path(run_dir), seen)
            if not commands:
                time.sleep(0.1)
                continue
            seen = commands[-1]["seq"]
            for command in commands:
                kind = command["type"]
                if kind == "route":
                    continuation = write_continuation(run_dir, command)
                    previous = {"model": state.get("model"), "effort": state.get("effort")}
                    backend.configure(command.get("model"), command.get("effort"))
                    if command.get("model"):
                        state["model"] = command["model"]
                    if command.get("effort"):
                        state["effort"] = command["effort"]
                    record_config_evidence(state, backend)
                    state["route_count"] = state.get("route_count", 0) + 1
                    state["last_compaction_id"] = command["compaction_id"]
                    state["continuation"] = str(continuation)
                    state.pop("route_recommendation", None)
                    prewalk_async.append_event(run_dir / "routes.jsonl",
                                               {"action": "route", "from_model": previous["model"],
                                                "from_effort": previous["effort"],
                                                "to_model": state.get("model"), "to_effort": state.get("effort"),
                                                "reason": command.get("reason") or "Navigator route",
                                                "compaction_id": command["compaction_id"],
                                                "same_session": True, "same_pid": True})
                    _write_state(run_dir, state)
                elif kind == "execute":
                    state.update(status="EXECUTING", phase="EXECUTING")
                    state["execution_count"] = state.get("execution_count", 0) + 1
                    state["last_execute_delta_chars"] = len(command.get("text", ""))
                    _write_state(run_dir, state)
                    backend.verify_integrity()
                    backend.prompt(phase_prompt(plan, run_dir, "EXECUTE", command.get("text", "")))
                    prewalk_layers.snapshot(run_dir / "worktree", run_dir, state["handoff_id"], None,
                                            "builder", "implementation and validation", skip_if_unchanged=True)
                    records = run_validation(run_dir, plan)
                    passed = bool(records) and all(record["exit_code"] == 0 for record in records)
                    signature = json.dumps([(r["command"], r["exit_code"]) for r in records], sort_keys=True)
                    if passed:
                        state.pop("failure_signature", None)
                        state.pop("repeated_failure_count", None)
                        state.pop("route_recommendation", None)
                    else:
                        same = state.get("failure_signature") == signature
                        state["repeated_failure_count"] = state.get("repeated_failure_count", 0) + 1 if same else 1
                        state["failure_signature"] = signature
                        if state["repeated_failure_count"] >= 2:
                            state["route_recommendation"] = "upgrade_builder_at_compaction"
                    state["status"] = "READY_FOR_REVIEW" if passed else "VALIDATION_FAILED"
                    state["phase"] = state["status"]
                    state["review_patch"] = str(run_dir / "builder-review.patch")
                    collect_usage(run_dir, state, state["execution_count"] + 1)
                    build_review(run_dir)
                    _write_state(run_dir, state)
                elif kind == "approve":
                    backend.verify_integrity()
                    apply_review(run_dir)
                    final_status = "approved"
                    break
                elif kind == "discard":
                    final_status = "discarded"
                    break
                elif kind == "cancel":
                    final_status = "cancelled"
                    break
            if final_status != "failed":
                break
    except Exception as exc:
        state["error"] = str(exc)
        try:
            prewalk_layers.snapshot(run_dir / "worktree", run_dir, state["handoff_id"], None,
                                    "supervisor", "final failure snapshot", skip_if_unchanged=True)
            build_review(run_dir)
        except Exception:
            pass
        final_status = "merge_conflict" if isinstance(exc, prewalk_layers.LayerError) and "apply" in str(exc) else "failed"
    finally:
        if backend is not None:
            backend.close()
        if final_status == "discarded":
            cleanup = subprocess.run(["git", "-C", state["source_worktree"], "worktree", "remove", "--force",
                                      str(run_dir / "worktree")], capture_output=True, check=False)
            if cleanup.returncode:
                state["cleanup_error"] = cleanup.stderr.decode("utf-8", "replace")
        state["status"] = final_status
        state["phase"] = final_status
        state["ended_at"] = _now()
        _write_state(run_dir, state)
        prewalk_async._release_active(run_dir)
        _event(run_dir, {"state": final_status, "error": state.get("error")})
    return 0


def status(run_dir: Path) -> dict[str, Any]:
    state = _read_state(run_dir)
    return {"ok": True, **state, "terminal": state["status"] in TERMINAL}


def wait(run_dir: Path, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        result = status(run_dir)
        if result["terminal"] or time.monotonic() >= deadline:
            return result
        time.sleep(0.2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("launch", "execute", "route", "status", "wait", "review", "approve", "discard", "cancel", "_supervise"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", required=True, type=Path)
        if name == "launch":
            p.add_argument("--plan", required=True, type=Path)
            p.add_argument("--backend", default="kimi-sub")
        if name == "execute":
            p.add_argument("--text", required=True)
        if name == "route":
            p.add_argument("--model")
            p.add_argument("--effort")
            p.add_argument("--reason", default="")
            p.add_argument("--compaction-id", required=True)
        if name == "wait":
            p.add_argument("--timeout", type=float, default=3600)
    args = parser.parse_args(argv)
    try:
        if args.command == "launch":
            result = launch(args.run_dir, args.plan, args.backend)
        elif args.command == "execute":
            result = queue(args.run_dir, "execute", args.text)
        elif args.command == "route":
            result = queue(args.run_dir, "route", model=args.model, effort=args.effort,
                           reason=args.reason, compaction_id=args.compaction_id)
        elif args.command == "status":
            result = status(args.run_dir)
        elif args.command == "wait":
            result = wait(args.run_dir, args.timeout)
        elif args.command == "review":
            result = build_review(args.run_dir)
        elif args.command == "approve":
            result = queue(args.run_dir, "approve")
        elif args.command == "discard":
            result = queue(args.run_dir, "discard")
        elif args.command == "cancel":
            result = queue(args.run_dir, "cancel")
        else:
            return supervise(args.run_dir)
    except (ValueError, OSError, persistent_backends.PersistentError, prewalk_layers.LayerError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
