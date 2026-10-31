#!/usr/bin/env python3
"""Fake Builder for tests: reads the brief on stdin, edits hello.txt in cwd,
appends progress, and returns a NAVIGATOR_REVIEW package in the generic envelope."""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--model", default="mock-ok")
p.add_argument("--turn", type=int, default=1)
p.add_argument("--session")
p.add_argument("--effort")
a = p.parse_args()

brief = sys.stdin.read()
m = re.search(r"^HANDOFF_ID:\s*(\S+)", brief, re.M)
handoff = m.group(1) if m else os.environ.get("PREWALK_HANDOFF_ID", "unknown")
# Leak check used by tests: the Builder must never see stripped API keys.
leaked = sorted(k for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY") if k in os.environ)

if a.model == "mock-rate-limit":
    print("You've hit your limit · resets 5pm (Asia/Singapore)", file=sys.stderr)
    sys.exit(1)
if a.model == "mock-fail":
    print("mock failure", file=sys.stderr)
    sys.exit(2)

session = a.session or f"mock-sess-{handoff}"
print(json.dumps({"type": "session", "session_id": session}), flush=True)
print(json.dumps({"type": "model", "model": a.model, "effort": a.effort}), flush=True)
if a.model == "mock-slow":
    time.sleep(float(os.environ.get("PREWALK_MOCK_SLOW_SECONDS", "30")))

target = Path.cwd() / "hello.txt"
with open(target, "a", encoding="utf-8") as fh:
    fh.write(f"hello from mock builder turn {a.turn}\n")
progress = os.environ.get("PREWALK_PROGRESS")
if progress:
    with open(progress, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"state": "step_completed", "summary": "wrote hello.txt",
                             "changed_paths": ["hello.txt"], "validation": "none",
                             "next": "return"}) + "\n")
if a.model == "mock-bad-return":
    text = "done without a review block"
else:
    text = ("Wrote hello.txt.\n```text\nPREWALK_PHASE: NAVIGATOR_REVIEW\nHANDOFF_ID: " + handoff +
            "\nSTATE: complete\nCHANGED: hello.txt\nVALIDATION: none\nRISKS: none\nLEAKED_ENV: " +
            (",".join(leaked) or "none") + "\n```")
print(json.dumps({"type": "final", "session_id": session, "text": text, "completed": True}), flush=True)
