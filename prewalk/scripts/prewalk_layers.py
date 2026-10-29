#!/usr/bin/env python3
"""Pure-git layer snapshots for Prewalk (no FUSE, no overlayfs).

Idea borrowed from LayerFS: the Navigator's first edit is an immutable layer
(L1) over the base (L0 = HEAD); every Builder step is captured as another
layer (L2-<n>) by the supervisor, so Navigator reviews layer evidence instead
of the Builder's self-report.

A snapshot never touches the user's branch, HEAD, index or worktree:
the real index is *copied* (stat cache reused, so only changed/untracked
files are rehashed) into a temporary GIT_INDEX_FILE, ``git add -A`` (honours
.gitignore) + ``write-tree`` + ``commit-tree`` build a commit object, and only
``refs/prewalk/<handoff-id>/<layer>`` is written.  L0 is HEAD itself: no scan.

Run-dir files: layers.json  first-edit.patch  review.md  review.json
review-L0-final.patch
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prewalk_runtime  # noqa: E402

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")
LAYER_RE = re.compile(r"^L[0-9][A-Za-z0-9_.-]{0,30}$")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
DEFAULT_DIFF_CHARS = 6000
_IDENT = {
    "GIT_AUTHOR_NAME": "prewalk", "GIT_AUTHOR_EMAIL": "prewalk@localhost",
    "GIT_COMMITTER_NAME": "prewalk", "GIT_COMMITTER_EMAIL": "prewalk@localhost",
}
TEMPLATE_INSTRUCTION = (
    "L1 is the TEMPLATE layer written by Navigator. Do not redo, revert or restyle it; "
    "extend the same pattern to the rest of FILE SCOPE. If L1 itself looks wrong, "
    "escalate instead of editing it. Every step you report is snapshotted from the "
    "worktree, so Navigator reviews the layers, not your summary."
)


class LayerError(Exception):
    pass


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


# ------------------------------------------------------------ git plumbing

def git(cwd: Path, *args: str, env: dict[str, str] | None = None,
        check: bool = True) -> str:
    full = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", **_IDENT, **(env or {})}
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          env=full, stdin=subprocess.DEVNULL)
    if check and proc.returncode != 0:
        raise LayerError(f"git {' '.join(args[:2])} failed: "
                         f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8", "surrogateescape")


def repo_root(cwd: Path) -> Path | None:
    try:
        return Path(git(cwd, "rev-parse", "--show-toplevel").strip())
    except (LayerError, OSError):
        return None


def _head(root: Path) -> str | None:
    out = git(root, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False).strip()
    return out or None


def _ref(handoff_id: str, layer: str) -> str:
    return f"refs/prewalk/{handoff_id}/{layer}"


def capture_tree(root: Path, scratch_dir: Path, seed: str = "index") -> str:
    """Tree of the current worktree (tracked + untracked, .gitignore honoured).

    ``seed="index"`` copies the real index (mtime preserved) so git's stat
    cache skips unchanged files; the real index is only read, never written.
    Falls back to ``read-tree HEAD`` when there is no index.  ``"empty"`` is
    for benchmarks only (rehashes every file).
    """
    scratch_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_index = tempfile.mkstemp(prefix=".prewalk-index-", dir=str(scratch_dir))
    os.close(fd)
    os.unlink(temp_index)  # git must create it, or read it from the copy below
    env = {"GIT_INDEX_FILE": temp_index}
    try:
        real_index = Path(git(root, "rev-parse", "--path-format=absolute",
                              "--git-path", "index").strip())
        if seed == "index" and real_index.exists():
            shutil.copy2(real_index, temp_index)
        elif seed != "empty" and _head(root):
            git(root, "read-tree", "HEAD", env=env)
        spec = ["."]
        with contextlib.suppress(ValueError):  # run dir inside the repo: never snapshot it
            rel = scratch_dir.resolve().relative_to(root.resolve()).as_posix()
            if rel != ".":
                spec.append(f":(top,exclude){rel}")
        git(root, "add", "-A", "--", *spec, env=env)
        return git(root, "write-tree", env=env).strip()
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_index)


# ------------------------------------------------------------ manifest / diff

def manifest(root: Path, a: str, b: str) -> list[dict[str, Any]]:
    """A/M/D per path with +/- line counts (binary: null counts)."""
    status = git(root, "diff", "--no-renames", "--name-status", "-z", a, b).split("\0")
    kinds = {status[i + 1]: status[i][0] for i in range(0, len(status) - 1, 2) if status[i]}
    stats: dict[str, tuple[int | None, int | None]] = {}
    for line in git(root, "diff", "--no-renames", "--numstat", "-z", a, b).split("\0"):
        parts = line.split("\t")
        if len(parts) == 3:
            added, deleted, path = parts
            stats[path] = (None if added == "-" else int(added),
                           None if deleted == "-" else int(deleted))
    return [{"path": p, "status": k, "added": stats.get(p, (None, None))[0],
             "deleted": stats.get(p, (None, None))[1]} for p, k in sorted(kinds.items())]


def totals(entries: list[dict[str, Any]]) -> dict[str, int]:
    return {"files": len(entries), "added": sum(e["added"] or 0 for e in entries),
            "deleted": sum(e["deleted"] or 0 for e in entries)}


def format_manifest(entries: list[dict[str, Any]], indent: str = "") -> list[str]:
    if not entries:
        return [f"{indent}(no changes)"]
    out = []
    for e in entries:
        counts = "binary" if e["added"] is None else f"+{e['added']} -{e['deleted']}"
        out.append(f"{indent}{e['status']} {e['path']} {counts}")
    return out


def capped(text: str, limit: int, full_hint: str) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    cut = cut[: cut.rfind("\n") + 1] or cut
    return (cut + f"[truncated: showing {len(cut)} of {len(text)} chars; "
                  f"full diff: {full_hint}]\n")


# ------------------------------------------------------------ layers.json

def layers_path(run_dir: Path) -> Path:
    return run_dir / "layers.json"


def load(run_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads(layers_path(run_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


@contextlib.contextmanager
def _locked(run_dir: Path) -> Iterator[None]:
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "layers.lock", "a+") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)


def next_builder_layer(doc: dict[str, Any] | None) -> str:
    n = sum(1 for l in (doc or {}).get("layers", []) if l["name"].startswith("L2-"))
    return f"L2-{n + 1}"


def snapshot(cwd: Path, run_dir: Path, handoff_id: str, layer: str | None = None,
             via: str = "navigator", message: str = "", extra: dict[str, Any] | None = None,
             skip_if_unchanged: bool = False) -> dict[str, Any]:
    """Capture the worktree as the next layer; returns the layer record.
    With ``skip_if_unchanged`` an unchanged worktree returns the last layer."""
    if not ID_RE.match(handoff_id):
        raise LayerError("invalid handoff_id")
    if via not in ("navigator", "builder", "supervisor"):
        raise LayerError("via must be navigator|builder|supervisor")
    root = repo_root(cwd)
    if root is None:
        raise LayerError(f"{cwd} is not inside a git worktree")
    run_dir = run_dir.resolve()
    with _locked(run_dir):
        doc = load(run_dir)
        if doc is None:
            head = _head(root)
            if head is None:  # unborn branch: base is an empty-tree commit object
                head = git(root, "commit-tree", EMPTY_TREE, "-m", "prewalk L0 (empty)").strip()
            git(root, "update-ref", _ref(handoff_id, "L0"), head)
            doc = {"schema_version": 1, "handoff_id": handoff_id, "repo": str(root),
                   "base": {"name": "L0", "sha": head, "ref": _ref(handoff_id, "L0"),
                            "head_at_start": _head(root)},
                   "layers": [], "errors": []}
        elif doc["handoff_id"] != handoff_id:
            raise LayerError(f"layers.json belongs to handoff {doc['handoff_id']}")
        layer = layer or next_builder_layer(doc)
        if not LAYER_RE.match(layer) or layer == "L0":
            raise LayerError(f"invalid layer name {layer!r}")
        if any(l["name"] == layer for l in doc["layers"]):
            raise LayerError(f"layer {layer} already exists")
        parent = doc["layers"][-1]["sha"] if doc["layers"] else doc["base"]["sha"]
        tree = capture_tree(root, run_dir)
        if skip_if_unchanged and doc["layers"] and doc["layers"][-1]["tree"] == tree:
            return doc["layers"][-1]
        sha = git(root, "commit-tree", tree, "-p", parent,
                  "-m", f"prewalk {handoff_id} {layer} ({via}): {message}"[:4000]).strip()
        git(root, "update-ref", _ref(handoff_id, layer), sha)
        entries = manifest(root, parent, sha)
        record = {"name": layer, "sha": sha, "tree": tree, "parent": parent, "via": via,
                  "message": message, "ts": _now(), "ref": _ref(handoff_id, layer),
                  "manifest": entries, "totals": totals(entries), **(extra or {})}
        doc["layers"].append(record)
        prewalk_runtime._atomic_write_json(layers_path(run_dir), doc)
        if layer == "L1":
            prewalk_runtime._atomic_write_text(
                run_dir / "first-edit.patch", git(root, "diff", "--no-renames", parent, sha))
        return record


def record_error(run_dir: Path, text: str) -> None:
    with _locked(run_dir):
        doc = load(run_dir)
        if doc is not None:
            doc.setdefault("errors", []).append({"ts": _now(), "error": text[:500]})
            prewalk_runtime._atomic_write_json(layers_path(run_dir), doc)


def _layer(doc: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((l for l in doc["layers"] if l["name"] == name), None)


# ------------------------------------------------------------ brief injection

def first_edit_section(run_dir: Path, max_chars: int = DEFAULT_DIFF_CHARS) -> str:
    doc = load(run_dir)
    l1 = doc and _layer(doc, "L1")
    if not l1:
        return ""
    root = Path(doc["repo"])
    l0 = doc["base"]["sha"]
    diff = git(root, "diff", "--no-renames", l0, l1["sha"])
    hint = f"{run_dir / 'first-edit.patch'} or `git diff {l0[:12]} {l1['sha'][:12]}`"
    lines = ["", f"FIRST EDIT = layer L1 ({l1['sha'][:12]}) over L0 = {l0[:12]}:",
             *format_manifest(l1["manifest"], "  "), TEMPLATE_INSTRUCTION,
             "L1 DIFF:", "```diff", capped(diff, max_chars, hint).rstrip("\n"), "```"]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ review

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _hunks(root: Path, a: str, b: str) -> dict[str, list[tuple[int, int, int, int]]]:
    out: dict[str, list[tuple[int, int, int, int]]] = {}
    path = None
    for line in git(root, "diff", "--no-renames", "-U0", a, b).splitlines():
        if line.startswith("+++ "):
            if line[4:] != "/dev/null":
                path = line[6:]
        elif line.startswith("--- ") and line[4:] != "/dev/null":
            path = line[6:]
        elif (m := _HUNK.match(line)) and path:
            o, oc, n, nc = (int(m.group(1)), int(m.group(2) or 1),
                            int(m.group(3)), int(m.group(4) or 1))
            out.setdefault(path, []).append((o, oc, n, nc))
    return out


def _interval(start: int, count: int) -> tuple[float, float]:
    # a zero-length hunk sits *between* lines: start and start+1
    return (start, start + count - 1) if count else (start + 0.5, start + 0.5)


def template_overlaps(root: Path, l0: str, l1: str, final: str) -> list[dict[str, Any]]:
    """Builder hunks (L1..final, old side) that hit first-edit lines (L0..L1, new side)."""
    first = _hunks(root, l0, l1)
    later = _hunks(root, l1, final)
    hits = []
    for path, tmpl in first.items():
        for (_, _, n, nc) in tmpl:
            if nc == 0:
                continue  # pure deletion in L1 leaves no template lines to protect
            ts, te = _interval(n, nc)
            for (o, oc, _, _) in later.get(path, []):
                bs, be = _interval(o, oc)
                if bs <= te and ts <= be:
                    hits.append({"path": path, "template_lines": f"{n}-{n + nc - 1}",
                                 "builder_lines_in_L1": f"{o}-{o + max(oc, 1) - 1}"})
    return hits


def in_scope(path: str, scope: list[str]) -> bool:
    for entry in scope:
        e = entry.strip().removeprefix("./")
        e = "" if e in (".", "/") else e.lstrip("/")
        if e == "" or path == e.rstrip("/"):
            return True
        if any(ch in e for ch in "*?[") and fnmatch.fnmatchcase(path, e):
            return True
        if path.startswith(e.rstrip("/") + "/"):
            return True
    return False


_KEY = re.compile(r"^\s*([A-Z][A-Z_ ]{2,30}):\s*(.*)$")


def claimed_paths(final_message: str) -> list[str] | None:
    """Paths listed under CHANGED: in the return package; None if absent."""
    lines = final_message.splitlines()
    for i, line in enumerate(lines):
        m = _KEY.match(line)
        if not m or m.group(1).strip() != "CHANGED":
            continue
        items = [m.group(2)] if m.group(2).strip() else []
        for nxt in lines[i + 1:]:
            if _KEY.match(nxt) or nxt.strip().startswith("```"):
                break
            items.append(nxt)
        found: list[str] = []
        for item in items:
            for chunk in item.split(","):
                chunk = chunk.strip().lstrip("-*").strip().strip("`")
                tok = re.match(r"([A-Za-z0-9_./-]+)", chunk)
                if tok and tok.group(1).lower() not in ("none", "n/a") and \
                        ("/" in tok.group(1) or "." in tok.group(1)):
                    found.append(tok.group(1).removeprefix("./"))
        return sorted(set(found))
    return None


def _latest_final_message(run_dir: Path) -> Path | None:
    finals = sorted(run_dir.glob("turn-*-final.txt"))
    return finals[-1] if finals else None


def review_report(run_dir: Path, scope: list[str] | None = None,
                  max_chars: int = DEFAULT_DIFF_CHARS,
                  state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write review.md/review.json: what the Builder actually did, from layers."""
    run_dir = run_dir.resolve()
    doc = load(run_dir)
    if not doc:
        raise LayerError(f"no layers.json in {run_dir}")
    root = Path(doc["repo"])
    brief = {}
    with contextlib.suppress(FileNotFoundError, json.JSONDecodeError):
        brief = json.loads((run_dir / "brief.json").read_text(encoding="utf-8"))
    if state is None:
        state = {}
        with contextlib.suppress(FileNotFoundError, json.JSONDecodeError):
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    scope = scope if scope is not None else brief.get("scope") or []
    l0 = doc["base"]["sha"]
    l1 = _layer(doc, "L1")
    final = doc["layers"][-1] if doc["layers"] else None
    flags: list[dict[str, Any]] = []
    if not l1 or not final:
        raise LayerError("need at least L1 to review")
    builder_layers = [l for l in doc["layers"] if l["name"].startswith("L2-")]
    builder = manifest(root, l1["sha"], final["sha"])
    overall = manifest(root, l0, final["sha"])
    if not builder_layers:
        flags.append({"flag": "no_builder_layers", "detail": "no L2 snapshot was taken"})
    if not builder:
        flags.append({"flag": "empty_after_L1",
                      "detail": "worktree after the Builder equals the first edit"})
    out = [e["path"] for e in builder if scope and not in_scope(e["path"], scope)]
    if out:
        flags.append({"flag": "out_of_scope", "paths": out, "scope": scope})
    overlaps = template_overlaps(root, l0, l1["sha"], final["sha"])
    if overlaps:
        flags.append({"flag": "template_edited", "hunks": overlaps})
    final_msg = _latest_final_message(run_dir)
    claim = None
    if final_msg is None:
        flags.append({"flag": "no_return_package",
                      "detail": f"status {state.get('status', '?')}; claim check skipped"})
    else:
        claim = claimed_paths(final_msg.read_text(encoding="utf-8", errors="replace"))
        actual = {e["path"] for e in builder}
        if claim is None:
            flags.append({"flag": "claim_missing", "detail": "return package has no CHANGED:"})
        else:
            first_edit_paths = {e["path"] for e in l1["manifest"]}
            not_done = sorted(set(claim) - actual - first_edit_paths)
            unreported = sorted(actual - set(claim))
            if not_done or unreported:
                flags.append({"flag": "claim_mismatch", "claimed_not_changed": not_done,
                              "changed_not_claimed": unreported})
    if doc.get("errors"):
        flags.append({"flag": "snapshot_errors", "errors": doc["errors"][-3:]})

    full_patch = run_dir / "review-L0-final.patch"
    prewalk_runtime._atomic_write_text(full_patch, git(root, "diff", "--no-renames", l0, final["sha"]))
    builder_diff = git(root, "diff", "--no-renames", l1["sha"], final["sha"])
    t_b, t_o = totals(builder), totals(overall)

    md = [f"# Prewalk review digest: {doc['handoff_id']}",
          f"Built from git layers refs/prewalk/{doc['handoff_id']}/*, not from the Builder's report.",
          f"Builder status: {state.get('status', 'unknown')} (turn {state.get('turn', '?')}), "
          f"exit {state.get('exit_code', '?')}. L0 = {l0[:12]}, final = {final['name']} {final['sha'][:12]}.",
          "", "## Flags"]
    if not flags:
        md.append("- none")
    for f in flags:
        body = {k: v for k, v in f.items() if k != "flag"}
        md.append(f"- **{f['flag']}**: {json.dumps(body, ensure_ascii=False)}")
    md += ["", "## Timeline"]
    for l in doc["layers"]:
        where = f" seq {l['progress_seq']}" if l.get("progress_seq") else ""
        t = l["totals"]
        md.append(f"- {l['name']} [{l['via']}{where}] {l['ts'][11:19]}Z \"{l['message'][:120]}\" "
                  f"{t['files']} files +{t['added']} -{t['deleted']}")
        md += format_manifest(l["manifest"], "    ")
    md += ["", "## Totals",
           f"- First edit (L0..L1): {l1['totals']['files']} files +{l1['totals']['added']} -{l1['totals']['deleted']}",
           f"- Builder (L1..{final['name']}): {t_b['files']} files +{t_b['added']} -{t_b['deleted']}",
           f"- Overall (L0..{final['name']}): {t_o['files']} files +{t_o['added']} -{t_o['deleted']}",
           *format_manifest(overall, "    "),
           "", f"## Builder diff (L1..{final['name']})", "```diff",
           capped(builder_diff, max_chars, f"`git diff {l1['sha'][:12]} {final['sha'][:12]}`").rstrip("\n")
           or "(empty)", "```",
           f"Full L0..final patch: {full_patch}"]
    report = {"ok": True, "handoff_id": doc["handoff_id"], "flags": flags, "status": state.get("status"),
              "layers": [l["name"] for l in doc["layers"]], "builder": builder, "overall": overall,
              "claimed": claim, "review_md": str(run_dir / "review.md"), "full_patch": str(full_patch)}
    prewalk_runtime._atomic_write_text(run_dir / "review.md", "\n".join(md) + "\n")
    prewalk_runtime._atomic_write_json(run_dir / "review.json", report)
    return report


def cleanup(run_dir: Path | None, cwd: Path | None, handoff_id: str) -> dict[str, Any]:
    """Delete refs/prewalk/<id>/*; objects are left to git gc. Run-dir files stay."""
    if not ID_RE.match(handoff_id):
        raise LayerError("invalid handoff_id")
    doc = load(run_dir.resolve()) if run_dir else None
    root = Path(doc["repo"]) if doc else repo_root(cwd or Path.cwd())
    if root is None:
        raise LayerError("cannot find the repository")
    refs = git(root, "for-each-ref", "--format=%(refname)", f"refs/prewalk/{handoff_id}/").split()
    for ref in refs:
        git(root, "update-ref", "-d", ref)
    return {"ok": True, "deleted": refs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("snapshot", help="capture the worktree as a layer")
    p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--handoff-id", required=True)
    p.add_argument("--cwd", type=Path, default=Path.cwd())
    p.add_argument("--layer", help="L1 for the first edit; default next L2-<n>")
    p.add_argument("--via", default="navigator", choices=("navigator", "builder", "supervisor"))
    p.add_argument("--message", default="")
    for name in ("review-report", "layer-diff"):
        p = sub.add_parser(name, help="write review.md for Navigator from the layers")
        p.add_argument("--run-dir", required=True, type=Path)
        p.add_argument("--scope", action="append", help="override brief scope (repeatable)")
        p.add_argument("--max-diff-chars", type=int, default=DEFAULT_DIFF_CHARS)
    p = sub.add_parser("cleanup", help="delete refs/prewalk/<id>/*")
    p.add_argument("--handoff-id", required=True)
    p.add_argument("--run-dir", type=Path)
    p.add_argument("--cwd", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            result = {"ok": True, **snapshot(args.cwd, args.run_dir, args.handoff_id,
                                             args.layer, args.via, args.message)}
        elif args.command in ("review-report", "layer-diff"):
            result = review_report(args.run_dir, args.scope, args.max_diff_chars)
            result = {k: result[k] for k in ("ok", "flags", "layers", "review_md", "full_patch")}
        else:
            result = cleanup(args.run_dir, args.cwd, args.handoff_id)
    except (LayerError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
