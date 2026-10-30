#!/usr/bin/env python3
"""Deterministic, snippet-free repository navigation for Prewalk."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

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


def build_index(cwd: Path, scopes: list[str], entry_terms: Iterable[str] = (),
                max_files: int = 200, max_entries: int = 500) -> dict[str, Any]:
    cwd = cwd.resolve()
    if prewalk_layers.repo_root(cwd) is None:
        raise ValueError(f"{cwd} is not a Git worktree")
    terms = [term for term in entry_terms if term]
    files = repository_files(cwd, scopes)
    entries: list[dict[str, Any]] = []
    truncated = False
    for path in files[:max_files]:
        try:
            lines = (cwd / path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        definition = DEFINITIONS.get(Path(path).suffix.lower())
        for number, line in enumerate(lines, 1):
            if len(entries) >= max_entries:
                truncated = True
                break
            match = definition.match(line) if definition else None
            if match:
                symbol = next((value for value in match.groups() if value), None)
                if symbol:
                    entries.append({"path": path, "line": number, "symbol": symbol,
                                    "relation": "definition", "note": f"{Path(path).suffix} symbol definition"})
            for term in terms:
                if term in line and (not match or term not in (match.groups() if match else ())):
                    entries.append({"path": path, "line": number, "symbol": term,
                                    "relation": "reference", "note": f"reference to {term}"})
        if truncated:
            break
    if len(files) > max_files or len(entries) >= max_entries:
        truncated = True
    tests = [path for path in files if is_test_path(path)]
    risks = []
    if len(files) > 20:
        risks.append("wide_scope")
    if not tests:
        risks.append("no_test_coverage")
    if truncated:
        risks.append("index_truncated")
    digest = {"schema_version": 1, "files": len(files),
              "definitions": sum(e["relation"] == "definition" for e in entries),
              "references": sum(e["relation"] == "reference" for e in entries),
              "test_files": tests[:20], "candidate_l1": entries[0] if entries else None,
              "risk_flags": risks,
              "uncertainties": ["index truncated; narrow scope"] if truncated else []}
    return {"schema_version": 1,
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "repo": str(cwd), "scope": scopes, "entry_terms": list(terms),
            "entries": entries, "truncated": truncated, "nav_digest": digest}


def write_index(run_dir: Path, document: dict[str, Any]) -> dict[str, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    index = run_dir / "exploration-index.json"
    digest = run_dir / "nav-digest.json"
    prewalk_runtime._atomic_write_json(index, document)
    prewalk_runtime._atomic_write_json(digest, document["nav_digest"])
    return {"index": index, "digest": digest}


def main(argv: list[str] | None = None) -> int:
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
