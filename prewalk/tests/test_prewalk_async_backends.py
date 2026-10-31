from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=ResourceWarning, message="subprocess")
sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import prewalk_async as ASYNC  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class BackendAsyncTests(unittest.TestCase):
    def setUp(self):
        warnings.simplefilter("ignore", ResourceWarning)  # detached supervisor
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = unittest.mock.patch.dict(os.environ, {
            "PREWALK_HOME": str(self.root / "home"), "PREWALK_MOCK_SLOW_SECONDS": "20",
            "ANTHROPIC_API_KEY": "SENTINEL-NOT-FOR-BUILDER", "OPENAI_API_KEY": "SENTINEL-NOT-FOR-BUILDER"})
        self.patch.start()
        self.wt = self.root / "wt"
        self.wt.mkdir()
        git(self.wt, "init", "-q")
        (self.wt / "a.txt").write_text("a\n")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", "init")
        self.runs = []

    def tearDown(self):
        for run in self.runs:
            st = ASYNC._read_json(run / "state.json", {})
            if st and st.get("status") not in ASYNC.TERMINAL:
                ASYNC.cancel(run, timeout=4)
        self.patch.stop()
        self.tmp.cleanup()

    def brief(self, name="brief.json", **extra):
        data = {"handoff_id": "h1", "backend": "mock", "goal": "write hello.txt", "layers": False,
                "success": ["hello.txt exists"], "scope": ["hello.txt"], "cwd": str(self.wt), **extra}
        p = self.root / name
        p.write_text(json.dumps(data))
        return p

    def run_dir(self, name="run"):
        r = (self.root / name).resolve()
        self.runs.append(r)
        return r

    def test_completed_records_backend_and_effort(self):
        run = self.run_dir()
        ASYNC.launch(run, self.brief(effort="max"))
        st = ASYNC.wait(run, 30)
        self.assertEqual(st["status"], "completed", st)
        self.assertEqual(st["backend"], "mock")
        self.assertEqual((st["requested_effort"], st["effective_effort"]), ("max", "high"))
        self.assertEqual(st["observed_models"], ["mock-ok"])
        state = json.loads((run / "state.json").read_text())
        self.assertEqual(state["host"], "generic")
        self.assertEqual(state["launch"]["cwd"], str(self.wt))
        self.assertNotIn("SENTINEL", (run / "state.json").read_text())
        final = (run / "turn-001-final.txt").read_text()
        self.assertIn("LEAKED_ENV: none", final)          # API keys stripped from the Builder env
        self.assertTrue((self.wt / "hello.txt").exists())
        started = [e for e in ASYNC.read_tail(run / "progress.jsonl") if e.get("state") == "started"][0]
        self.assertIn("effort requested max -> effective high", started["summary"])
        self.assertFalse(any((self.root / "home" / "active").glob("*.json")))

    def test_resume_turn_uses_recorded_session(self):
        run = self.run_dir()
        ASYNC.launch(run, self.brief())
        first = ASYNC.wait(run, 30)
        (run / "turn-002-delta.txt").write_text("HANDOFF_ID: h1\nalso add a second line\n")
        ASYNC.launch(run, run / "brief.json", turn=2, expected_session=first["session_id"])
        st = ASYNC.wait(run, 30)
        self.assertEqual(st["status"], "completed", st)
        self.assertIn("--session", json.loads((run / "state.json").read_text())["launch"]["argv"])

    def test_rate_limit_is_retryable_waiting_state_then_retry(self):
        run = self.run_dir()
        ASYNC.launch(run, self.brief(model="mock-rate-limit"))
        st = ASYNC.wait(run, 30)
        self.assertEqual(st["status"], "waiting_rate_limit")
        self.assertTrue(st["retryable"])
        self.assertIn("hit your limit", st["limit_message"])
        out = ASYNC.retry(run, model="mock-ok")
        self.assertEqual(out["attempt"], 2)
        st = ASYNC.wait(run, 30)
        self.assertEqual(st["status"], "completed", st)
        self.assertTrue((run / "turn-001-attempt-1.err").exists())
        state = json.loads((run / "state.json").read_text())
        self.assertEqual(state["attempts"][0]["status"], "waiting_rate_limit")
        with self.assertRaises(ASYNC.AsyncError):
            ASYNC.retry(run)                               # completed turns are not retryable

    def test_failure_that_is_not_a_limit_stays_failed(self):
        run = self.run_dir()
        ASYNC.launch(run, self.brief(model="mock-fail"))
        self.assertEqual(ASYNC.wait(run, 30)["status"], "failed")

    def test_concurrency_one_and_cancel(self):
        run_a, run_b = self.run_dir("ra"), self.run_dir("rb")
        ASYNC.launch(run_a, self.brief(model="mock-slow"))
        deadline = time.monotonic() + 10
        while ASYNC.status(run_a)["status"] != "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        with self.assertRaisesRegex(ASYNC.AsyncError, "concurrency limit 1"):
            ASYNC.launch(run_b, self.brief("b.json", handoff_id="h2"))
        out = ASYNC.cancel(run_a, timeout=6)
        self.assertEqual(out["status"], "cancelled", out)
        ASYNC.launch(run_b, self.brief("b.json", handoff_id="h2"))   # slot released
        self.assertEqual(ASYNC.wait(run_b, 30)["status"], "completed")

    def test_simultaneous_launches_atomically_share_one_slot(self):
        run_a, run_b = self.run_dir("race-a"), self.run_dir("race-b")
        briefs = [self.brief("race-a.json", model="mock-slow"),
                  self.brief("race-b.json", handoff_id="h2", model="mock-slow")]
        barrier = threading.Barrier(2)
        launched, errors = [], []

        def compete(run, brief):
            barrier.wait(timeout=10)
            try:
                ASYNC.launch(run, brief)
                launched.append(run)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=compete, args=(run, brief))
                   for run, brief in zip((run_a, run_b), briefs)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(len(launched), 1, errors)
        self.assertEqual(len(errors), 1)
        self.assertIn("concurrency limit 1", str(errors[0]))
        ASYNC.cancel(launched[0], timeout=6)

    def test_literal_argv_overrides_backend(self):
        run = self.run_dir()
        fake = self.root / "fake.py"
        fake.write_text('import json,sys; sys.stdin.read(); print(json.dumps({"type":"final","session_id":"x",'
                        '"text":"```text\\nPREWALK_PHASE: NAVIGATOR_REVIEW\\nHANDOFF_ID: h1\\n```","completed":True}))')
        ASYNC.launch(run, self.brief(argv=[sys.executable, str(fake)], host="generic"))
        st = ASYNC.wait(run, 30)
        self.assertEqual(st["status"], "completed", st)
        self.assertNotIn("backend", json.loads((run / "state.json").read_text()))

    def test_brief_validation_for_backends(self):
        base = {"handoff_id": "h", "goal": "g", "success": ["s"], "scope": ["x"]}
        for bad, msg in (({"backend": "nope"}, "unknown Builder backend"),
                         ({"backend": "pi-api"}, "not implemented"),
                         ({"backend": "claude-code-sub", "model": "opus"}, "not allowed as Builder"),
                         ({"backend": "mock", "effort": "ultra"}, "unknown effort"),
                         ({"backend": "mock", "allowed_bash": "make"}, "allowed_bash"),
                         ({}, "or backend is required")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ASYNC.AsyncError, msg):
                ASYNC.validate_brief({**base, **bad})
        ASYNC.validate_brief({**base, "backend": "codex:deepseek"})


if __name__ == "__main__":
    unittest.main()
