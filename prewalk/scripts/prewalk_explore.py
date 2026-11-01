#!/usr/bin/env python3
"""Deterministic, snippet-free repository navigation for Prewalk.

Definitions come from ``ast`` for Python and from Universal Ctags (when on
PATH) for other languages, falling back to regex.  References come from
``git grep -w -F --untracked``, falling back to a line scan.
"""

from __future__ import annotations

import argparse
import ast
import datetime as _dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import prewalk_hash
import prewalk_layers
import prewalk_runtime

DEFINITIONS = {
    ".py": re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)"),
    ".js": re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)"),
    ".ts": re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|type)\s+([A-Za-z_$][\w$]*)"),
    ".tsx": re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|type)\s+([A-Za-z_$][\w$]*)"),
    ".jsx": re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)"),
    ".java": re.compile(r"^\s*(?:public|protected|private|abstract|final|static|\s)*\s*(?:class|interface|enum|record)\s+(\w+)"),
    ".kt": re.compile(r"^\s*(?:data\s+|sealed\s+|open\s+|abstract\s+)?(?:class|interface|object|fun)\s+(\w+)"),
}
TEST_PARTS = {"test", "tests", "spec", "specs", "__tests__", "androidtest"}


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          stdin=subprocess.DEVNULL, check=False)
    if proc.returncode != 0:
        raise ValueError(proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout.decode("utf-8", "surrogateescape")


def repository_files(cwd: Path, scopes: list[str]) -> list[str]:
    tracked = _git(cwd, "ls-files", "-z").split("\0")
    untracked = _git(cwd, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted({path for path in [*tracked, *untracked]
                   if path and prewalk_layers.in_scope(path, scopes)})


def is_test_path(path: str) -> bool:
    parts = {part.lower() for part in Path(path).parts}
    name = Path(path).name.lower()
    return bool(parts & TEST_PARTS) or name.startswith("test_") or ".test." in name or ".spec." in name


def _universal_ctags() -> str | None:
    exe = shutil.which("ctags")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                              timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return exe if "Universal Ctags" in (proc.stdout + proc.stderr) else None


def _py_definitions(text: str) -> list[dict[str, Any]] | None:
    """AST definitions with qualified names and spans; None on SyntaxError."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    found: list[dict[str, Any]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}" if prefix else child.name
                found.append({"symbol": name, "line": child.lineno,
                              "end_line": getattr(child, "end_lineno", None) or child.lineno})
                visit(child, f"{name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


CTAGS_BATCH = 100


def _ctags_definitions(exe: str, cwd: Path,
                       paths: list[str]) -> dict[str, list[dict[str, Any]]] | None:
    """One ctags invocation for a batch of files; results mapped back by the
    tag's `path` field (normalized to forward-slash relative paths)."""
    try:
        proc = subprocess.run(
            [exe, "--output-format=json", "--fields=+nKe", "--sort=no", "-f", "-",
             *[str(Path(p)) for p in paths]],
            capture_output=True, text=True, timeout=120, cwd=cwd, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    lookup = {Path(rel).as_posix(): rel for rel in paths}
    found: dict[str, list[dict[str, Any]]] = {rel: [] for rel in paths}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            tag = json.loads(line)
        except json.JSONDecodeError:
            continue
        if tag.get("_type") != "tag" or not tag.get("name") or not tag.get("line"):
            continue
        rel = lookup.get(Path(str(tag.get("path", ""))).as_posix())
        if rel is None:
            rel = lookup.get(Path(str(tag.get("path", ""))).as_posix().removeprefix("./"))
        if rel is None:
            continue
        entry = {"symbol": str(tag["name"]), "line": int(tag["line"])}
        if isinstance(tag.get("end"), int):
            entry["end_line"] = tag["end"]
        found[rel].append(entry)
    return found


def _regex_definitions(suffix: str, lines: list[str]) -> list[dict[str, Any]]:
    pattern = DEFINITIONS.get(suffix)
    if not pattern:
        return []
    found = []
    for number, line in enumerate(lines, 1):
        match = pattern.match(line)
        if match:
            symbol = next((value for value in match.groups() if value), None)
            if symbol:
                found.append({"symbol": symbol, "line": number})
    return found


def _file_definitions(path: str, lines: list[str]) -> list[dict[str, Any]]:
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        parsed = _py_definitions("\n".join(lines))
        if parsed is not None:
            return parsed
    return _regex_definitions(suffix, lines)


def _git_grep_references(cwd: Path, term: str, scopes: list[str]) -> list[tuple[str, int]] | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "grep", "-n", "-w", "-F", "--untracked", "-e", term],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1):
        return None
    hits = []
    for line in proc.stdout.decode("utf-8", "surrogateescape").splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        path = parts[0]
        try:
            number = int(parts[1])
        except ValueError:
            continue
        if prewalk_layers.in_scope(path, scopes):
            hits.append((path, number))
    return hits


def _scan_references(lines_by_file: dict[str, list[str]], term: str) -> list[tuple[str, int]]:
    hits = []
    for path, lines in lines_by_file.items():
        for number, line in enumerate(lines, 1):
            if term in line:
                hits.append((path, number))
    return hits


def _rank_candidates(entries: list[dict[str, Any]], terms: Iterable[str]) -> list[dict[str, Any]]:
    term_set = set(terms)
    references = [e for e in entries if e["relation"] == "reference"]
    scored = []
    for entry in entries:
        if entry["relation"] != "definition":
            continue
        score = 0
        symbol = entry["symbol"]
        leaf = symbol.rsplit(".", 1)[-1]
        if symbol in term_set or leaf in term_set:
            score += 100
        same_file = [r for r in references if r["path"] == entry["path"]]
        end = entry.get("end_line")
        if end is not None:
            score += 10 * sum(1 for r in same_file if entry["line"] <= r["line"] <= end)
        else:
            score += sum(1 for r in same_file)
        if is_test_path(entry["path"]):
            score -= 5
        scored.append((-score, entry["path"], entry["line"], {**entry, "score": score}))
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in scored[:3]]


def build_index(cwd: Path, scopes: list[str], entry_terms: Iterable[str] = (),
                max_files: int = 200, max_entries: int = 500) -> dict[str, Any]:
    cwd = cwd.resolve()
    if prewalk_layers.repo_root(cwd) is None:
        raise ValueError(f"{cwd} is not a Git worktree")
    terms = [term for term in entry_terms if term]
    files = repository_files(cwd, scopes)
    ctags = _universal_ctags()

    lines_by_file: dict[str, list[str]] = {}
    definitions: list[dict[str, Any]] = []
    truncated = len(files) > max_files
    non_py: list[tuple[str, list[str]]] = []
    for path in files[:max_files]:
        try:
            lines = (cwd / path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        lines_by_file[path] = lines
        if Path(path).suffix.lower() == ".py" or not ctags:
            for found in _file_definitions(path, lines):
                definitions.append({"path": path, "line": found["line"],
                                    "symbol": found["symbol"], "relation": "definition",
                                    "end_line": found.get("end_line"),
                                    "note": f"{Path(path).suffix} symbol definition"})
        else:
            non_py.append((path, lines))
    if ctags and non_py:
        for start in range(0, len(non_py), CTAGS_BATCH):
            batch = non_py[start:start + CTAGS_BATCH]
            parsed = _ctags_definitions(ctags, cwd, [path for path, _ in batch])
            for path, lines in batch:
                found_defs = parsed.get(path) if parsed is not None else None
                if found_defs is None:
                    found_defs = _regex_definitions(Path(path).suffix.lower(), lines)
                for found in found_defs:
                    definitions.append({"path": path, "line": found["line"],
                                        "symbol": found["symbol"], "relation": "definition",
                                        "end_line": found.get("end_line"),
                                        "note": f"{Path(path).suffix} symbol definition"})

    def_lines = {(e["path"], e["line"]) for e in definitions}
    references: list[dict[str, Any]] = []
    reference_engine = "git-grep"
    for term in terms:
        hits = _git_grep_references(cwd, term, scopes)
        if hits is None:
            reference_engine = "scan"
            hits = _scan_references(lines_by_file, term)
        for path, number in hits:
            if (path, number) in def_lines:
                continue
            references.append({"path": path, "line": number, "symbol": term,
                               "relation": "reference", "note": f"reference to {term}"})

    entries = sorted(definitions, key=lambda e: (e["path"], e["line"])) + \
        sorted(references, key=lambda e: (e["path"], e["line"]))
    # Rank candidates over the full, untruncated entry list.
    candidates = _rank_candidates(entries, terms)
    if len(entries) > max_entries:
        truncated = True
    # Truncation keeps all references first, then fills with definitions,
    # preferring definitions in files that contain references.
    ref_files = {e["path"] for e in references}
    kept = sorted(references, key=lambda e: (e["path"], e["line"]))[:max_entries]
    kept += sorted(
        definitions,
        key=lambda e: (e["path"] not in ref_files,
                        e["path"], e["line"]))[:max(0, max_entries - len(kept))]
    tests = [path for path in files if is_test_path(path)]
    risks = []
    if len(files) > 20:
        risks.append("wide_scope")
    if not tests:
        risks.append("no_test_coverage")
    if truncated:
        risks.append("index_truncated")
    digest = {"schema_version": 1, "files": len(files),
              "definitions": sum(e["relation"] == "definition" for e in kept),
              "references": sum(e["relation"] == "reference" for e in kept),
              "test_files": tests[:20],
              "engine": {"definitions": "ast+ctags" if ctags else "ast+regex",
                         "references": reference_engine},
              "candidates": candidates,
              "candidate_l1": candidates[0] if candidates else None,
              "risk_flags": risks,
              "uncertainties": ["index truncated; narrow scope"] if truncated else []}
    return {"schema_version": 1,
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "repo": str(cwd), "scope": scopes, "entry_terms": list(terms),
            "entries": kept, "truncated": truncated, "nav_digest": digest}


def write_index(run_dir: Path, document: dict[str, Any]) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    index = run_dir / "exploration-index.json"
    digest = run_dir / "nav-digest.json"
    index_unchanged, fingerprint = prewalk_hash.write_json_if_changed(index, document, {"generated_at"})
    digest_unchanged, _ = prewalk_hash.write_json_if_changed(digest, document["nav_digest"])
    return {"index": index, "digest": digest, "fingerprint": fingerprint,
            "unchanged": index_unchanged and digest_unchanged}


def query_index(run_dir: Path, symbol: str | None = None, path: str | None = None,
                relation: str | None = None, limit: int = 20) -> dict[str, Any]:
    index = run_dir / "exploration-index.json"
    document = json.loads(prewalk_runtime._read_text_with_retry(index))
    entries = [e for e in document.get("entries", []) if isinstance(e, dict)]
    if relation:
        entries = [e for e in entries if e.get("relation") == relation]
    if path:
        entries = [e for e in entries if str(e.get("path", "")).startswith(path)]
    if symbol:
        exact = [e for e in entries if e.get("symbol") == symbol]
        substring = [e for e in entries
                     if e.get("symbol") != symbol and symbol in str(e.get("symbol", ""))]
        entries = exact + substring
    total = len(entries)
    entries = entries[:limit]
    compact = [{k: v for k, v in e.items() if k != "note"} for e in entries]
    return {"ok": True, "total": total, "returned": len(compact), "entries": compact}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "query":
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("command")
        parser.add_argument("--run-dir", required=True, type=Path)
        parser.add_argument("--symbol")
        parser.add_argument("--path")
        parser.add_argument("--relation")
        parser.add_argument("--limit", type=int, default=20)
        args = parser.parse_args(argv)
        try:
            result = query_index(args.run_dir, args.symbol, args.path, args.relation, args.limit)
        except (ValueError, OSError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--scope", action="append", default=[])
    parser.add_argument("--entry", action="append", default=[])
    parser.add_argument("--max-files", type=int, default=200)
    parser.add_argument("--max-entries", type=int, default=500)
    args = parser.parse_args(argv)
    try:
        document = build_index(args.cwd, args.scope or ["."], args.entry,
                               args.max_files, args.max_entries)
        paths = write_index(args.run_dir, document)
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **{key: str(value) for key, value in paths.items()},
                      "nav_digest": document["nav_digest"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
