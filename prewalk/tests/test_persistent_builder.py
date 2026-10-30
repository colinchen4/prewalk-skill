from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import prewalk_builder
import prewalk_layers
import prewalk_plan


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env=env).stdout


class PersistentBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.run = self.root / "run"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "test")
        git(self.repo, "config", "user.email", "test@example.com")
        (self.repo / "hello.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.head = git(self.repo, "rev-parse", "HEAD").strip()
        self.index = (self.repo / ".git/index").read_bytes()
        self.env = mock.patch.dict(os.environ, {
            "PREWALK_HOME": str(self.root / "home"),
            "GIT_CEILING_DIRECTORIES": str(self.root),
        })
        self.env.start()

    def tearDown(self):
        try:
            state_path = self.run / "state.json"
            if state_path.exists():
                state = json.loads(state_path.read_text())
                if state["status"] not in prewalk_builder.TERMINAL:
                    prewalk_builder.queue(self.run, "cancel")
                    prewalk_builder.wait(self.run, 5)
                if (self.run / "worktree").exists():
                    prewalk_builder.queue(self.run, "discard")
        except Exception:
            pass
        self.env.stop()
        self.tmp.cleanup()

    def wait_status(self, target: str, timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = prewalk_builder.status(self.run)
            if state["status"] == target:
                return state
            if state["status"] in {"failed", "merge_conflict", "cancelled"} and state["status"] != target:
                self.fail(f"unexpected state: {state}")
            time.sleep(0.05)
        self.fail(f"status {target} not reached: {prewalk_builder.status(self.run)}")

    def make_plan(self, mode="brief_only", validation=None):
        expected = "base\nhello from builder\n" if mode == "brief_only" else "first edit by navigator\nhello from builder\n"
        command = validation or (
            f'"{sys.executable}" -c "from pathlib import Path; '
            f'assert Path(\'hello.txt\').read_text() == {expected!r}"'
        )
        plan = prewalk_plan.create_plan("h1", mode, "Append hello from builder to hello.txt",
                                        ["exact file content"], ["hello.txt"], cwd=str(self.repo),
                                        validation=[command])
        return prewalk_plan.write_plan(self.run, plan)

    def execute_and_wait_ready(self):
        state = self.wait_status("AWAITING_NAVIGATOR")
        prewalk_builder.queue(self.run, "execute", "continue with the approved plan")
        self.wait_status("READY_FOR_REVIEW")
        return state

    def test_brief_only_uses_same_session_and_merges_after_approval(self):
        self.make_plan()
        launched = prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        first = self.execute_and_wait_ready()
        state = prewalk_builder.status(self.run)
        self.assertEqual(first["builder_pid"], state["builder_pid"])
        self.assertEqual(first["session_id"], state["session_id"])
        self.assertEqual(state["cold_starts"], 1)
        review = prewalk_builder.build_review(self.run)
        self.assertTrue(review["has_diff"])
        self.assertTrue(review["validation_passed"])
        prewalk_builder.queue(self.run, "approve")
        self.wait_status("approved")
        self.assertEqual((self.repo / "hello.txt").read_text(), "base\nhello from builder\n")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").strip(), self.head)
        self.assertEqual((self.repo / ".git/index").read_bytes(), self.index)
        trace = json.loads((self.run / "mock-session.json").read_text())
        self.assertEqual([p["phase"] for p in trace["prompts"]],
                         ["PREWALK_PHASE: EXPLORING", "PREWALK_PHASE: EXECUTE"])
        self.assertEqual(trace["session_id"], state["session_id"])
        self.assertEqual(trace["pid"], state["builder_pid"])
        self.assertEqual(launched["supervisor_pid"], state["supervisor_pid"])

    def test_sample_guided_retains_complete_l1_patch(self):
        prewalk_layers.snapshot(self.repo, self.run, "h1", "L0", "navigator", "inherited baseline")
        (self.repo / "hello.txt").write_text("first edit by navigator\n", encoding="utf-8")
        prewalk_layers.snapshot(self.repo, self.run, "h1", "L1", "navigator", "sample edit")
        self.make_plan("sample_guided")
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.execute_and_wait_ready()
        self.assertIn("first edit by navigator", (self.run / "first-edit.patch").read_text())
        prewalk_builder.queue(self.run, "approve")
        self.wait_status("approved")
        self.assertEqual((self.repo / "hello.txt").read_text(),
                         "first edit by navigator\nhello from builder\n")

    def test_route_at_compaction_keeps_pid_and_session(self):
        path = self.make_plan()
        raw = json.loads(path.read_text())
        raw.update(model="mock-ok", effort="low")
        path.write_text(json.dumps(raw), encoding="utf-8")
        prewalk_builder.launch(self.run, path, "mock")
        before = self.wait_status("AWAITING_NAVIGATOR")
        with self.assertRaises(ValueError):
            prewalk_builder.queue(self.run, "route", model="mock-strong", effort="high")
        prewalk_builder.queue(self.run, "route", model="mock-strong", effort="high",
                              reason="compaction upgrade", compaction_id="compact-1")
        deadline = time.monotonic() + 10
        while prewalk_builder.status(self.run).get("route_count", 0) < 1:
            if time.monotonic() > deadline:
                self.fail("route not applied")
            time.sleep(0.05)
        after = prewalk_builder.status(self.run)
        self.assertEqual(before["builder_pid"], after["builder_pid"])
        self.assertEqual(before["session_id"], after["session_id"])
        self.assertEqual(after["cold_starts"], 1)
        self.assertEqual(after["model"], "mock-strong")
        self.assertEqual(after["effort"], "high")
        self.assertTrue((self.run / "continuation-compact-1.json").exists())
        trace = json.loads((self.run / "mock-session.json").read_text())
        self.assertEqual(trace["model"], "mock-strong")
        self.assertEqual(trace["thinking"], "high")

    def test_repeated_failure_recommends_compaction_upgrade(self):
        self.make_plan(validation=f'"{sys.executable}" -c "raise SystemExit(1)"')
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.wait_status("AWAITING_NAVIGATOR")
        for attempt in range(1, 3):
            prewalk_builder.queue(self.run, "execute", "retry the same failure")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                current = prewalk_builder.status(self.run)
                if current["status"] == "VALIDATION_FAILED" and current.get("execution_count") == attempt:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"execution {attempt} did not finish: {current}")
        state = prewalk_builder.status(self.run)
        self.assertEqual(state["repeated_failure_count"], 2)
        self.assertEqual(state["route_recommendation"], "upgrade_builder_at_compaction")

    def test_navigator_only_never_launches_builder(self):
        plan = prewalk_plan.create_plan("h1", "navigator_only", "do it directly", ["done"],
                                        ["hello.txt"], cwd=str(self.repo), validation=[])
        path = prewalk_plan.write_plan(self.run, plan)
        with self.assertRaises(ValueError):
            prewalk_builder.launch(self.run, path, "mock")
        self.assertFalse((self.run / "worktree").exists())

    def test_validation_failure_blocks_merge_and_allows_discard(self):
        self.make_plan(validation=f'"{sys.executable}" -c "raise SystemExit(1)"')
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.wait_status("AWAITING_NAVIGATOR")
        prewalk_builder.queue(self.run, "execute", "implement")
        self.wait_status("VALIDATION_FAILED")
        with self.assertRaises(ValueError):
            prewalk_builder.queue(self.run, "approve")
        self.assertEqual((self.repo / "hello.txt").read_text(), "base\n")
        self.assertTrue((self.run / "worktree").exists())
        prewalk_builder.queue(self.run, "discard")
        self.wait_status("discarded")
        self.assertFalse((self.run / "worktree").exists())

    def test_execution_failure_preserves_worktree_until_discard(self):
        self.make_plan()
        os.environ["PREWALK_MOCK_FAIL"] = "1"
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.wait_status("AWAITING_NAVIGATOR")
        prewalk_builder.queue(self.run, "execute", "implement")
        self.wait_status("failed")
        self.assertTrue((self.run / "worktree").exists())
        self.assertEqual((self.repo / "hello.txt").read_text(), "base\n")
        prewalk_builder.queue(self.run, "discard")
        self.assertFalse((self.run / "worktree").exists())

    def test_merge_conflict_preserves_source_and_failed_layer(self):
        self.make_plan()
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.execute_and_wait_ready()
        (self.repo / "hello.txt").write_text("source changed independently\n", encoding="utf-8")
        prewalk_builder.queue(self.run, "approve")
        self.wait_status("merge_conflict")
        self.assertEqual((self.repo / "hello.txt").read_text(), "source changed independently\n")
        self.assertTrue((self.run / "worktree").exists())
        prewalk_builder.queue(self.run, "discard")


if __name__ == "__main__":
    unittest.main()
