#!/usr/bin/env python3
"""Opt-in asynchronous (non-blocking) Builder supervision for Prewalk.

Serial blocking Prewalk stays the default.  With this module Navigator starts
the single Builder under a detached supervisor and returns immediately, so it
can keep planning, answer the user, or review landed edits while the Builder
runs.  ``progress.jsonl`` and ``state.json`` keep updating after the parent
turn ends.  Navigator checks back with ``status`` (non-blocking, tail only),
redirects with ``steer``, stops with ``cancel``, or blocks with ``wait``.

The Builder argv comes from the selected adapter document; this module never
invents host commands.  Run-directory files (per Builder turn N)::

    brief.txt  steer.md  progress.jsonl  state.json  cancel.requested
    turn-NNN-events.jsonl  turn-NNN.err  turn-NNN-final.txt  usage.json
    layers.json  first-edit.patch  review.md  review.json  (git layers, see
    prewalk_layers.py: L1 = first edit, L2-<n> = one per Builder step)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:  # POSIX advisory lock; elsewhere single-line O_APPEND writes are used.
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prewalk_runtime  # noqa: E402
import prewalk_layers  # noqa: E402

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")
MAX_CONTEXT_CHARS = 1500
TERMINAL = {"completed", "failed", "cancelled", "invalid_return"}
HOSTS = ("kimi", "codex", "claude-code", "dsh", "generic")
POLL_SECONDS = 0.1


class AsyncError(Exception):
    pass


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _write_json(path: Path, value: Any) -> None:
    prewalk_runtime._atomic_write_json(path, value)


def paths_for(run_dir: Path, turn: int = 1) -> dict[str, Path]:
    n = f"{turn:03d}"
    return {
        "run_dir": run_dir,
        "brief": run_dir / ("brief.txt" if turn == 1 else f"turn-{n}-delta.txt"),
        "steer": run_dir / "steer.md",
        "progress": run_dir / "progress.jsonl",
        "state": run_dir / "state.json",
        "cancel": run_dir / "cancel.requested",
        "events": run_dir / f"turn-{n}-events.jsonl",
        "err": run_dir / f"turn-{n}.err",
        "final": run_dir / f"turn-{n}-final.txt",
        "usage": run_dir / "usage.json",
    }


# ------------------------------------------------------------ progress

def append_event(progress: Path, event: dict[str, Any]) -> dict[str, Any]:
    """Append one JSON line under a lock; ``seq`` continues across writers."""
    progress.parent.mkdir(parents=True, exist_ok=True)
    with open(progress, "a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.seek(0)
            seq = sum(1 for line in handle if line.strip()) + 1
            record = {"ts": _now(), "seq": seq, **event}
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)
    return record


def read_tail(progress: Path, after_seq: int = 0) -> list[dict[str, Any]]:
    """Events with seq > after_seq.  Lines are numbered by position, so a
    Builder line without ``seq`` still gets a stable cursor."""
    if not progress.exists():
        return []
    out = []
    for index, line in enumerate(
        (l for l in progress.read_text(encoding="utf-8").splitlines() if l.strip()), 1
    ):
        if index <= after_seq:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            record = {"malformed": True}
        if not isinstance(record, dict):
            record = {"malformed": True}
        record.setdefault("seq", index)
        out.append(record)
    return out


# ------------------------------------------------------------ brief

def validate_brief(brief: Any) -> dict[str, Any]:
    if not isinstance(brief, dict):
        raise AsyncError("brief must be a JSON object")
    if not isinstance(brief.get("handoff_id"), str) or not ID_RE.match(brief["handoff_id"]):
        raise AsyncError("handoff_id is required")
    if not isinstance(brief.get("goal"), str) or not brief["goal"].strip():
        raise AsyncError("goal is required")
    for key in ("success", "scope"):
        value = brief.get(key)
        if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
            raise AsyncError(f"{key} must be a non-empty list of strings")
    context = brief.get("context", "")
    if not isinstance(context, str) or len(context) > MAX_CONTEXT_CHARS:
        raise AsyncError(f"context must be a string of at most {MAX_CONTEXT_CHARS} chars")
    argv = brief.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise AsyncError("argv (taken from the adapter document) is required")
    if brief.get("host", "generic") not in HOSTS:
        raise AsyncError(f"unsupported host {brief.get('host')!r}")
    return brief


def render_brief(brief: dict[str, Any], paths: dict[str, Path], first_edit: str = "") -> str:
    lines = [
        "PREWALK_PHASE: BUILDER_EXECUTE",
        f"HANDOFF_ID: {brief['handoff_id']}",
        "",
        f"GOAL: {brief['goal'].strip()}",
        "SUCCESS:", *[f"- {s}" for s in brief["success"]],
        "FILE SCOPE:", *[f"- {s}" for s in brief["scope"]],
    ]
    if brief.get("context"):
        lines += ["CONTEXT:", brief["context"].strip()]
    if first_edit:
        lines.append(first_edit.rstrip("\n"))
    lines += [
        "",
        f"PROGRESS: append one JSON line per step to {paths['progress']}: "
        '{"state","summary","changed_paths","validation","next"} (no secrets, no raw output).',
        f"STEER: before each step read {paths['steer']} if it exists; it overrides this brief.",
        "RETURN: short status, then a fenced block starting `PREWALK_PHASE: NAVIGATOR_REVIEW`, "
        "this HANDOFF_ID, STATE (complete|blocked|escalated), CHANGED, VALIDATION (command: result), "
        "RISKS. Escalate only for requirement or architecture decisions. Do not review your own work.",
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ process control

def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _layers_enabled(brief: dict[str, Any]) -> bool:
    return brief.get("layers", True) is not False and prewalk_layers.repo_root(
        Path(brief.get("cwd") or os.getcwd())) is not None


def _prepare_layers(run_dir: Path, brief: dict[str, Any]) -> str:
    """Snapshot the first edit as L1 (unless Navigator already did) and render
    the manifest + capped L0..L1 diff that is appended to the Builder brief."""
    if not _layers_enabled(brief):
        return ""
    try:
        doc = prewalk_layers.load(run_dir)
        if not (doc and prewalk_layers._layer(doc, "L1")):
            prewalk_layers.snapshot(Path(brief.get("cwd") or os.getcwd()), run_dir,
                                    brief["handoff_id"], "L1", "navigator",
                                    brief.get("first_edit_note") or "first edit (captured at launch)")
        return prewalk_layers.first_edit_section(
            run_dir, int(brief.get("first_edit_diff_chars", prewalk_layers.DEFAULT_DIFF_CHARS)))
    except prewalk_layers.LayerError as exc:
        raise AsyncError(f"first-edit snapshot failed: {exc}") from exc


def _snapshot_steps(run_dir: Path, cwd: Path, handoff_id: str, turn: int, seen: int) -> int:
    """One builder layer per batch of new Builder progress lines (supervisor
    side, so layers exist even if the Builder later crashes or is cancelled)."""
    events = read_tail(run_dir / "progress.jsonl", seen)
    if not events:
        return seen
    steps = [e for e in events if e.get("source") not in ("supervisor", "navigator")]
    if steps:
        claimed = sorted({p for e in steps for p in (e.get("changed_paths") or [])
                          if isinstance(p, str)})
        message = "; ".join(str(e.get("summary") or e.get("state") or "step")
                            for e in steps)[:300]
        try:
            prewalk_layers.snapshot(cwd, run_dir, handoff_id, None, "builder", message,
                                    {"turn": turn, "progress_seq": steps[-1]["seq"],
                                     "progress_changed_paths": claimed})
        except (prewalk_layers.LayerError, OSError) as exc:
            prewalk_layers.record_error(run_dir, f"step snapshot: {exc}")
    return events[-1]["seq"]


def _final_layers(run_dir: Path, cwd: Path, state: dict[str, Any], seen: int) -> None:
    seen = _snapshot_steps(run_dir, cwd, state["handoff_id"], state["turn"], seen)
    try:
        prewalk_layers.snapshot(cwd, run_dir, state["handoff_id"], None, "supervisor",
                                f"final: Builder {state['status']} (exit {state.get('exit_code')})",
                                {"turn": state["turn"], "final": True}, skip_if_unchanged=True)
    except (prewalk_layers.LayerError, OSError) as exc:
        prewalk_layers.record_error(run_dir, f"final snapshot: {exc}")
    try:
        state["review_report"] = prewalk_layers.review_report(run_dir, state=state)["review_md"]
    except (prewalk_layers.LayerError, OSError) as exc:
        state["review_report_error"] = str(exc)


def launch(run_dir: Path, brief_path: Path, turn: int = 1,
           expected_session: str | None = None) -> dict[str, Any]:
    """Start the Builder in the background and return immediately."""
    run_dir = run_dir.resolve()
    brief = validate_brief(_read_json(brief_path))
    paths = paths_for(run_dir, turn)
    state = _read_json(paths["state"], {})
    if state and state.get("status") not in TERMINAL:
        raise AsyncError("a Builder turn is already running in this run dir")
    if turn > 1 and not expected_session:
        raise AsyncError("--expected-session is required for turn > 1 (resume, never cold-start)")
    for key in ("events", "err", "final"):
        if paths[key].exists():
            raise AsyncError(f"{paths[key].name} exists; earlier turns are never overwritten")
    run_dir.mkdir(parents=True, exist_ok=True)
    if paths["cancel"].exists():
        paths["cancel"].unlink()
    if turn == 1:
        first_edit = _prepare_layers(run_dir, brief)
        prewalk_runtime._atomic_write_text(paths["brief"], render_brief(brief, paths, first_edit))
    elif not paths["brief"].exists():
        raise AsyncError(f"write the resume delta to {paths['brief']} first")
    _write_json(run_dir / "brief.json", brief)
    state = {"handoff_id": brief["handoff_id"], "host": brief.get("host", "generic"),
             "turn": turn, "expected_session": expected_session or state.get("session_id"),
             "session_id": state.get("session_id"), "status": "starting", "launched_at": _now()}
    _write_json(paths["state"], state)
    sup = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_supervise",
         "--run-dir", str(run_dir), "--turn", str(turn)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )
    return {"ok": True, "run_dir": str(run_dir), "turn": turn, "supervisor_pid": sup.pid,
            "brief": str(paths["brief"]), "progress": str(paths["progress"]),
            "next": "keep working; check back with `status --after <last_seq>`"}


def supervise(run_dir: Path, turn: int) -> int:
    brief = _read_json(run_dir / "brief.json")
    paths = paths_for(run_dir, turn)
    state = _read_json(paths["state"])
    cwd = brief.get("cwd") or os.getcwd()
    mapping = {k: str(v) for k, v in paths.items()}
    mapping.update(cwd=cwd, session=state.get("expected_session") or "")
    argv = [a.format(**mapping) for a in (brief["argv"] if turn == 1 else brief.get("resume_argv") or [])]
    if not argv:
        state.update(status="failed", error="resume_argv missing from brief", ended_at=_now())
        _write_json(paths["state"], state)
        return 0
    stdin = open(paths["brief"], "rb") if brief.get("stdin_brief", True) else subprocess.DEVNULL
    layers_on = (run_dir / "layers.json").exists() and brief.get("layers", True) is not False
    seen = len(read_tail(paths["progress"]))  # before Popen: no Builder line is missed
    try:
        with open(paths["events"], "wb") as out, open(paths["err"], "wb") as err:
            proc = subprocess.Popen(argv, cwd=cwd, stdin=stdin, stdout=out, stderr=err,
                                    start_new_session=True)
            state.update(status="running", supervisor_pid=os.getpid(), builder_pid=proc.pid,
                         started_at=_now())
            _write_json(paths["state"], state)
            append_event(paths["progress"], {"handoff_id": state["handoff_id"], "turn": turn,
                                             "source": "supervisor", "state": "started",
                                             "summary": f"Builder pid {proc.pid}"})
            while True:
                try:
                    code = proc.wait(timeout=POLL_SECONDS)
                except subprocess.TimeoutExpired:
                    code = None
                if layers_on:
                    seen = _snapshot_steps(run_dir, Path(cwd), state["handoff_id"], turn, seen)
                if code is not None:
                    break
    finally:
        if hasattr(stdin, "close"):
            stdin.close()
    state.update(exit_code=code, ended_at=_now())
    if paths["cancel"].exists():
        state["status"], summary = "cancelled", "Navigator cancelled the turn; work preserved"
    elif code != 0:
        state["status"], summary = "failed", f"Builder exited {code}"
    else:
        try:
            result = prewalk_runtime.inspect(
                host=state["host"], events_path=paths["events"], final_message_path=paths["final"],
                usage_path=paths["usage"], builder_turn=turn, cwd=Path(cwd),
                expected_session=state.get("expected_session") if turn > 1 else None,
                handoff_id=state["handoff_id"],
            )
            state.update(status="completed", session_id=result["session_id"],
                         usage_status=result["usage_status"])
            summary = "Builder returned a NAVIGATOR_REVIEW package"
        except (prewalk_runtime.InspectError, OSError) as exc:
            state.update(status="invalid_return", error=str(exc))
            summary = f"return rejected: {exc}"
    if layers_on:
        _final_layers(run_dir, Path(cwd), state, seen)
        if state.get("review_report"):
            summary += f"; layer review: {state['review_report']}"
    _write_json(paths["state"], state)
    append_event(paths["progress"], {"handoff_id": state["handoff_id"], "turn": turn,
                                     "source": "supervisor", "state": state["status"],
                                     "summary": summary})
    return 0


def status(run_dir: Path, after_seq: int = 0) -> dict[str, Any]:
    """Non-blocking snapshot: current state plus only the new progress tail."""
    paths = paths_for(run_dir)
    state = _read_json(paths["state"])
    if not state:
        raise AsyncError(f"no Builder run in {run_dir}")
    tail = read_tail(paths["progress"], after_seq)
    result = {"ok": True, "status": state["status"], "turn": state.get("turn"),
              "terminal": state["status"] in TERMINAL, "new_events": tail,
              "last_seq": tail[-1]["seq"] if tail else after_seq}
    if state["status"] == "running" and not _pid_alive(state.get("supervisor_pid")):
        result["note"] = "supervisor not alive; inspect raw turn artifacts"
    for key in ("error", "session_id", "exit_code", "review_report"):
        if state.get(key) is not None:
            result[key] = state[key]
    if result["terminal"] and state["status"] == "completed":
        result["final_message"] = str(paths_for(run_dir, state["turn"])["final"])
    return result


def steer(run_dir: Path, text: str) -> dict[str, Any]:
    """Soft redirect: the Builder reads steer.md before its next step."""
    paths = paths_for(run_dir)
    if not paths["state"].exists():
        raise AsyncError(f"no Builder run in {run_dir}")
    prewalk_runtime._atomic_write_text(paths["steer"], text.rstrip() + "\n")
    append_event(paths["progress"], {"source": "navigator", "state": "steered",
                                     "summary": text.strip()[:200]})
    return {"ok": True, "steer": str(paths["steer"])}


def cancel(run_dir: Path, timeout: float = 10.0) -> dict[str, Any]:
    """Hard stop of the running turn; the worktree is left as is."""
    paths = paths_for(run_dir)
    state = _read_json(paths["state"])
    if not state:
        raise AsyncError(f"no Builder run in {run_dir}")
    if state["status"] in TERMINAL:
        return {"ok": True, "status": state["status"], "note": "already terminal"}
    paths["cancel"].write_text(_now(), encoding="utf-8")
    deadline = time.monotonic() + timeout
    while not isinstance((state := _read_json(paths["state"])).get("builder_pid"), int):
        if time.monotonic() > deadline:
            return {"ok": False, "status": state["status"], "note": "Builder never started"}
        time.sleep(0.05)
    sig = signal.SIGTERM
    while True:
        try:
            if hasattr(os, "killpg"):
                os.killpg(state["builder_pid"], sig)
            else:  # pragma: no cover
                os.kill(state["builder_pid"], sig)
        except ProcessLookupError:
            pass
        end = time.monotonic() + (timeout / 2)
        while time.monotonic() < end:
            state = _read_json(paths["state"])
            if state["status"] in TERMINAL:
                return {"ok": True, "status": state["status"]}
            time.sleep(0.05)
        if sig == signal.SIGKILL or not hasattr(signal, "SIGKILL"):
            return {"ok": False, "status": state["status"], "note": "supervisor has not confirmed"}
        sig = signal.SIGKILL


def wait(run_dir: Path, timeout: float) -> dict[str, Any]:
    """Blocking compatibility path (the serial mode's behaviour)."""
    deadline = time.monotonic() + timeout
    while True:
        result = status(run_dir)
        if result["terminal"] or time.monotonic() > deadline:
            return result
        time.sleep(0.2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("launch"); p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--brief", required=True, type=Path)
    p.add_argument("--turn", type=int, default=1); p.add_argument("--expected-session")
    p = sub.add_parser("status"); p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--after", type=int, default=0, help="last seq already read")
    p = sub.add_parser("steer"); p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--text", required=True)
    p = sub.add_parser("cancel"); p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--timeout", type=float, default=10.0)
    p = sub.add_parser("wait"); p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--timeout", type=float, default=3600.0)
    p = sub.add_parser("review"); p.add_argument("--run-dir", required=True, type=Path)
    p = sub.add_parser("_supervise"); p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--turn", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        if args.command == "launch":
            result = launch(args.run_dir, args.brief, args.turn, args.expected_session)
        elif args.command == "status":
            result = status(args.run_dir, args.after)
        elif args.command == "steer":
            result = steer(args.run_dir, args.text)
        elif args.command == "cancel":
            result = cancel(args.run_dir, args.timeout)
        elif args.command == "wait":
            result = wait(args.run_dir, args.timeout)
        elif args.command == "review":
            report = prewalk_layers.review_report(args.run_dir)
            result = {k: report[k] for k in ("ok", "flags", "layers", "review_md")}
        else:
            return supervise(args.run_dir, args.turn)
    except (AsyncError, prewalk_layers.LayerError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
