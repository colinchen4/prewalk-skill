from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import warnings

# launch() intentionally detaches the supervisor; its Popen handle is never waited.
warnings.filterwarnings("ignore", category=ResourceWarning, message="subprocess")
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "prewalk_async.py"
SPEC = importlib.util.spec_from_file_location("prewalk_async_under_test", SCRIPT)
ASYNC = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ASYNC)

# A fake Builder that speaks the generic normalized envelope on stdout,
# appends its own progress lines, and honours steer.md between steps.
FAKE_BUILDER = r'''
import json, sys, time, pathlib
progress, steer, steps, delay, final_id = sys.argv[1], pathlib.Path(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
sys.stdin.read()
print(json.dumps({"type": "session", "session_id": "sess-1"}), flush=True)
seen = ""
for i in range(steps):
    if steer.exists():
        seen = steer.read_text().strip()
    with open(progress, "a") as fh:
        fh.write(json.dumps({"state": "step_completed", "summary": f"step {i}", "steer": seen}) + "\n")
    time.sleep(delay)
text = "done\n```text\nPREWALK_PHASE: NAVIGATOR_REVIEW\nHANDOFF_ID: " + final_id + "\nSTEER: " + seen + "\n```"
print(json.dumps({"type": "final", "session_id": "sess-1", "text": text, "completed": True}), flush=True)
'''


class AsyncTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        self.fake = self.root / "fake_builder.py"
        self.fake.write_text(FAKE_BUILDER, encoding="utf-8")

    def tearDown(self) -> None:
        state = ASYNC._read_json(self.run / "state.json", {})
        if state and state.get("status") not in ASYNC.TERMINAL and self.run.exists():
            ASYNC.cancel(self.run, timeout=4)
        self.temp.cleanup()

    def brief(self, steps=3, delay=0.2, final_id="h1", **extra) -> Path:
        data = {
            "handoff_id": "h1",
            "host": "generic",
            "layers": False,
            "goal": "Finish the pattern from the first edit",
            "success": ["unit tests pass"],
            "scope": ["src/"],
            "cwd": str(self.root),
            "argv": [sys.executable, str(self.fake), "{progress}", "{steer}",
                     str(steps), str(delay), final_id],
            **extra,
        }
        path = self.root / "brief.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def wait_for(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail("condition not reached")


class AsyncLaunchTests(AsyncTestBase):
    def test_launch_returns_before_builder_finishes(self):
        start = time.monotonic()
        result = ASYNC.launch(self.run, self.brief(steps=10, delay=0.2))
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertTrue(result["ok"])
        snap = ASYNC.status(self.run)
        self.assertFalse(snap["terminal"])
        self.assertIn(snap["status"], {"starting", "running"})

    def test_status_is_non_blocking_and_returns_only_new_tail(self):
        ASYNC.launch(self.run, self.brief(steps=6, delay=0.15))
        first = self.wait_for(lambda: (s := ASYNC.status(self.run))["new_events"] and s)
        t0 = time.monotonic()
        snap = ASYNC.status(self.run, first["last_seq"])
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertTrue(all(e["seq"] > first["last_seq"] for e in snap["new_events"]))
        done = ASYNC.wait(self.run, timeout=15)
        self.assertEqual(done["status"], "completed")
        all_events = ASYNC.status(self.run)["new_events"]
        self.assertEqual([e["seq"] for e in all_events], list(range(1, len(all_events) + 1)))
        self.assertEqual(all_events[-1]["source"], "supervisor")
        self.assertEqual(all_events[-1]["state"], "completed")

    def test_progress_keeps_updating_after_parent_process_exits(self):
        # The "parent turn" is a separate CLI process that launches and exits.
        brief = self.brief(steps=5, delay=0.2)
        parent = subprocess.run(
            [sys.executable, str(SCRIPT), "launch", "--run-dir", str(self.run), "--brief", str(brief)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(parent.returncode, 0, parent.stderr)
        lines_at_exit = len(ASYNC.read_tail(self.run / "progress.jsonl"))
        done = ASYNC.wait(self.run, timeout=15)
        self.assertEqual(done["status"], "completed")
        self.assertGreater(len(ASYNC.read_tail(self.run / "progress.jsonl")), lines_at_exit)
        usage = json.loads((self.run / "usage.json").read_text(encoding="utf-8"))
        self.assertEqual(usage["turn_count"], 1)

    def test_one_running_turn_per_run_dir(self):
        ASYNC.launch(self.run, self.brief(steps=10, delay=0.2))
        with self.assertRaises(ASYNC.AsyncError):
            ASYNC.launch(self.run, self.brief())

    def test_wrong_handoff_id_in_return_is_rejected(self):
        ASYNC.launch(self.run, self.brief(steps=1, delay=0.01, final_id="other"))
        done = ASYNC.wait(self.run, timeout=10)
        self.assertEqual(done["status"], "invalid_return")
        self.assertIn("HANDOFF_ID", done["error"])

    def test_pid_alive_accepts_current_process_on_windows(self):
        self.assertTrue(ASYNC._pid_alive(os.getpid()))

    def test_missing_builder_executable_reaches_failed_state(self):
        path = self.brief(argv=[str(self.root / "missing-builder-executable")])
        ASYNC.launch(self.run, path)
        done = ASYNC.wait(self.run, timeout=10)
        self.assertEqual(done["status"], "failed")
        self.assertIn("could not start Builder", done["error"])


class RedirectAndCancelTests(AsyncTestBase):
    def test_steer_reaches_running_builder(self):
        ASYNC.launch(self.run, self.brief(steps=8, delay=0.15))
        self.wait_for(lambda: ASYNC.status(self.run)["status"] == "running")
        ASYNC.steer(self.run, "Use the helper in src/util.py instead")
        done = ASYNC.wait(self.run, timeout=15)
        self.assertEqual(done["status"], "completed")
        final = Path(done["final_message"]).read_text(encoding="utf-8")
        self.assertIn("STEER: Use the helper in src/util.py instead", final)
        events = ASYNC.read_tail(self.run / "progress.jsonl")
        self.assertTrue(any(e.get("state") == "steered" for e in events))

    def test_cancel_stops_builder_and_records_state(self):
        ASYNC.launch(self.run, self.brief(steps=200, delay=0.1))
        self.wait_for(lambda: ASYNC.status(self.run)["status"] == "running")
        pid = ASYNC._read_json(self.run / "state.json")["builder_pid"]
        result = ASYNC.cancel(self.run, timeout=6)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "cancelled")
        self.wait_for(lambda: not ASYNC._pid_alive(pid))
        self.assertFalse((self.run / "turn-001-final.txt").exists())
        self.assertEqual(ASYNC.read_tail(self.run / "progress.jsonl")[-1]["state"], "cancelled")

    def test_cancel_terminal_run_is_noop(self):
        ASYNC.launch(self.run, self.brief(steps=1, delay=0.01))
        ASYNC.wait(self.run, timeout=10)
        self.assertEqual(ASYNC.cancel(self.run)["note"], "already terminal")

    def test_resume_requires_expected_session_and_delta(self):
        ASYNC.launch(self.run, self.brief(steps=1, delay=0.01))
        ASYNC.wait(self.run, timeout=10)
        with self.assertRaises(ASYNC.AsyncError):
            ASYNC.launch(self.run, self.brief(), turn=2)
        with self.assertRaises(ASYNC.AsyncError):
            ASYNC.launch(self.run, self.brief(), turn=2, expected_session="sess-1")


class BriefTests(AsyncTestBase):
    def test_brief_is_short_and_carries_return_contract(self):
        ASYNC.launch(self.run, self.brief(steps=1, delay=0.01))
        text = (self.run / "brief.txt").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("PREWALK_PHASE: BUILDER_EXECUTE\nHANDOFF_ID: h1\n"))
        self.assertIn("PREWALK_PHASE: NAVIGATOR_REVIEW", text)
        self.assertLess(len(text), 2000)
        ASYNC.wait(self.run, timeout=10)

    def test_brief_validation(self):
        for bad in ({"goal": ""}, {"success": []}, {"scope": "src"}, {"argv": []},
                    {"context": "x" * (ASYNC.MAX_CONTEXT_CHARS + 1)}, {"host": "nope"}):
            path = self.brief()
            data = json.loads(path.read_text()); data.update(bad); path.write_text(json.dumps(data))
            with self.assertRaises(ASYNC.AsyncError, msg=bad):
                ASYNC.launch(self.run, path)


if __name__ == "__main__":
    unittest.main()
