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
        for _ in range(25):
            try:
                self.tmp.cleanup()
                return
            except OSError:
                time.sleep(0.2)

    def wait_file(self, path: Path, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.05)
        self.fail(f"{path} not written")

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
        os.environ["ANTHROPIC_API_KEY"] = "sentinel"
        os.environ["DEEPSEEK_API_KEY"] = "sentinel"
        self.make_plan()
        launched = prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        first = self.execute_and_wait_ready()
        state = prewalk_builder.status(self.run)
        self.assertEqual(first["builder_pid"], state["builder_pid"])
        self.assertEqual(first["session_id"], state["session_id"])
        self.assertEqual(state["cold_starts"], 1)
        self.assertEqual(state["integrity_status"], "verified")
        self.assertEqual(len(state["cli_integrity"]["sha256"]), 64)
        review = prewalk_builder.build_review(self.run)
        self.assertTrue(review["has_diff"])
        self.assertTrue(review["validation_passed"])
        repeated = prewalk_builder.build_review(self.run)
        self.assertTrue(repeated["patch_unchanged"])
        self.assertEqual(repeated["patch_sha256"], review["patch_sha256"])
        prewalk_builder.queue(self.run, "approve")
        self.wait_status("approved")
        self.assertEqual((self.repo / "hello.txt").read_text(), "base\nhello from builder\n")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").strip(), self.head)
        self.assertEqual((self.repo / ".git/index").read_bytes(), self.index)
        trace = json.loads((self.run / "mock-session.json").read_text())
        self.assertEqual([p["phase"] for p in trace["prompts"]],
                         ["PREWALK_PHASE: EXECUTE"])
        self.assertEqual(trace["session_id"], state["session_id"])
        self.assertEqual(trace["pid"], state["builder_pid"])
        self.assertEqual(trace["leaked_env"], [])
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
        self.assertEqual(after["model_status"], "verified")
        self.assertEqual(after["config_evidence"]["model"], "mock-strong")
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


    def _validation(self, expected="base\nhello from builder\n"):
        return (f'"{sys.executable}" -c "from pathlib import Path; '
                f'assert Path(\'hello.txt\').read_text() == {expected!r}"')

    def make_explore_plan(self):
        plan = prewalk_plan.create_plan("h1", "brief_only",
                                        "Append hello from builder to hello.txt",
                                        ["exact file content"], ["hello.txt"], cwd=str(self.repo),
                                        validation=[self._validation()], explore=True)
        return prewalk_plan.write_plan(self.run, plan)

    def cli(self, *argv: str) -> dict:
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = prewalk_builder.main(list(argv))
        self.assertEqual(code, 0, out.getvalue())
        return json.loads(out.getvalue())

    def test_explore_opt_in_records_reply_and_contradictions(self):
        os.environ["PREWALK_MOCK_CONTRADICT"] = "scope lists hello.txt but plan says goodbye.txt"
        self.make_explore_plan()
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.wait_file(self.run / "turn-001-reply.md")
        state = self.wait_status("AWAITING_NAVIGATOR")
        deadline = time.monotonic() + 10
        while "exploration_contradictions" not in state and time.monotonic() < deadline:
            time.sleep(0.05)
            state = prewalk_builder.status(self.run)
        self.assertTrue((self.run / "turn-001-reply.md").exists())
        self.assertEqual(state["exploration_contradictions"],
                         ["scope lists hello.txt but plan says goodbye.txt"])
        prewalk_builder.queue(self.run, "execute", "continue with the approved plan")
        self.wait_status("READY_FOR_REVIEW")
        trace = json.loads((self.run / "mock-session.json").read_text())
        self.assertEqual([p["phase"] for p in trace["prompts"]],
                         ["PREWALK_PHASE: EXPLORING", "PREWALK_PHASE: EXECUTE"])
        prewalk_builder.queue(self.run, "approve")
        self.wait_status("approved")

    def test_second_execute_prompt_is_delta_only(self):
        self.make_plan()
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.execute_and_wait_ready()
        prewalk_builder.queue(self.run, "execute", "append once more")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            current = prewalk_builder.status(self.run)
            if current.get("execution_count", 0) >= 2 and current["status"] in prewalk_builder.WAITING:
                break
            time.sleep(0.05)
        trace = json.loads((self.run / "mock-session.json").read_text())
        self.assertEqual(len(trace["prompts"]), 2)
        self.assertTrue(trace["prompts"][0]["goal"])
        self.assertFalse(trace["prompts"][1]["goal"])
        state = prewalk_builder.status(self.run)
        self.assertGreater(state["prompt_chars_total"], 0)
        self.assertLess(state["last_prompt_chars"], trace["prompts"][0]["chars"])

    def test_early_execute_with_cli_wait_returns_compact_summary(self):
        self.make_plan()
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        result = self.cli("execute", "--run-dir", str(self.run), "--text", "go",
                          "--wait", "--timeout", "60")
        self.assertEqual(result["status"], "READY_FOR_REVIEW")
        self.assertTrue(result["idle"])
        self.assertTrue(result["has_diff"])
        self.assertEqual(result["execution_count"], 1)
        self.assertEqual(result["handled_command_seq"], 1)
        self.assertEqual(len(result["validation"]), 1)
        self.assertEqual(result["validation"][0]["exit_code"], 0)
        result = self.cli("approve", "--run-dir", str(self.run), "--wait", "--timeout", "60")
        self.assertEqual(result["status"], "approved")
        self.assertTrue(result["idle"])

    def test_wait_returns_promptly_when_idle(self):
        self.make_plan()
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        start = time.monotonic()
        result = prewalk_builder.wait(self.run, 30)
        self.assertLess(time.monotonic() - start, 5)
        self.assertTrue(result["idle"])
        self.assertEqual(result["status"], "AWAITING_NAVIGATOR")

    def test_builder_note_surfaces_on_validation_failure(self):
        self.make_plan(validation=f'"{sys.executable}" -c "raise SystemExit(1)"')
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.wait_status("AWAITING_NAVIGATOR")
        prewalk_builder.queue(self.run, "execute", "implement")
        state = self.wait_status("VALIDATION_FAILED")
        self.assertEqual(state["builder_note"],
                         "appended hello from builder to hello.txt\nall plan constraints were respected")
        self.assertIn("builder_note", prewalk_builder.summary(self.run))
        self.assertTrue((self.run / "turn-001-reply.md").exists())

    def test_idle_timeout_fails_the_run(self):
        os.environ["PREWALK_BUILDER_IDLE_TIMEOUT"] = "1"
        os.environ["PREWALK_MOCK_SLOW"] = "5"
        self.make_plan()
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.wait_status("AWAITING_NAVIGATOR")
        prewalk_builder.queue(self.run, "execute", "implement")
        state = self.wait_status("failed")
        self.assertIn("timed out", state["error"])

    def test_stream_activity_resets_idle_deadline(self):
        os.environ["PREWALK_BUILDER_IDLE_TIMEOUT"] = "1"
        os.environ["PREWALK_MOCK_STREAM"] = "4"
        self.make_plan()
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.execute_and_wait_ready()
        state = prewalk_builder.status(self.run)
        self.assertEqual(state["status"], "READY_FOR_REVIEW")

    def test_cancel_during_turn_ends_cancelled_and_keeps_worktree(self):
        os.environ["PREWALK_MOCK_SLOW"] = "30"
        os.environ["PREWALK_CANCEL_GRACE"] = "3"
        self.make_plan()
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.wait_status("AWAITING_NAVIGATOR")
        prewalk_builder.queue(self.run, "execute", "implement")
        deadline = time.monotonic() + 10
        while prewalk_builder.status(self.run)["status"] != "EXECUTING":
            if time.monotonic() > deadline:
                self.fail("never started executing")
            time.sleep(0.05)
        prewalk_builder.queue(self.run, "cancel")
        state = self.wait_status("cancelled", timeout=15)
        self.assertTrue((self.run / "worktree").exists())
        self.assertGreaterEqual(state["handled_command_seq"], 2)
        self.assertTrue(prewalk_builder.is_idle(self.run))

    def test_navigator_calls_and_record_usage_merge(self):
        self.make_plan()
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.wait_status("AWAITING_NAVIGATOR")
        self.cli("status", "--run-dir", str(self.run))
        calls = [json.loads(line) for line in
                 (self.run / "navigator-calls.jsonl").read_text().splitlines() if line.strip()]
        self.assertTrue(any(c["command"] == "status" and c["stdout_chars"] > 0 for c in calls))
        prewalk_builder.record_usage(self.run, "navigator", 100, 20, model="nav/m")
        prewalk_builder.record_usage(self.run, "navigator", 50, 10, cache_read=5)
        usage = json.loads((self.run / "usage.json").read_text())
        nav = usage["navigator"]
        self.assertEqual(nav["usage_status"], "recorded")
        self.assertEqual(nav["totals"]["inputOther"], 150)
        self.assertEqual(nav["totals"]["output"], 30)
        self.assertEqual(nav["totals"]["inputCacheRead"], 5)
        self.assertEqual(nav["models"], ["nav/m"])
        self.assertEqual(nav["call_count"], 2)

    def test_approved_run_writes_savings_report(self):
        self.make_plan()
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.execute_and_wait_ready()
        prewalk_builder.queue(self.run, "approve")
        self.wait_status("approved")
        self.wait_file(self.run / "savings.json")
        savings = json.loads((self.run / "savings.json").read_text())
        self.assertEqual(savings["mode"], "unpriced")
        self.assertIn("tokens", savings)
        self.assertTrue((self.run / "savings.md").exists())
        summary = prewalk_builder.summary(self.run)
        self.assertEqual(summary["savings"]["mode"], "unpriced")
        history = Path(os.environ["PREWALK_HOME"]) / "savings-history.jsonl"
        self.assertTrue(history.exists())

    def test_corrupt_prices_file_does_not_change_run_status(self):
        prices = self.root / "prices.json"
        prices.write_text("{corrupt", encoding="utf-8")
        os.environ["PREWALK_PRICES_PATH"] = str(prices)
        self.make_plan()
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.execute_and_wait_ready()
        prewalk_builder.queue(self.run, "approve")
        state = self.wait_status("approved")
        self.wait_file(self.run / "savings.json")
        savings = json.loads((self.run / "savings.json").read_text())
        self.assertTrue(savings["errors"])
        self.assertEqual(state["status"], "approved")


    def usage_turns(self) -> list[int]:
        log = self.root / "usage-turns.log"
        os.environ["PREWALK_USAGE_TURN_LOG"] = str(log)
        return log

    def read_turns(self, log: Path) -> list[int]:
        if not log.exists():
            return []
        return [int(line) for line in log.read_text().split() if line.strip()]

    def test_collect_usage_uses_current_turn_number(self):
        log = self.usage_turns()
        self.make_plan()
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.execute_and_wait_ready()
        self.assertEqual(self.read_turns(log), [1])
        prewalk_builder.queue(self.run, "execute", "second pass")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            current = prewalk_builder.status(self.run)
            if current.get("execution_count", 0) >= 2                     and current["status"] in prewalk_builder.WAITING:
                break
            time.sleep(0.05)
        self.assertEqual(self.read_turns(log), [1, 2])

    def test_collect_usage_turns_with_explore(self):
        log = self.usage_turns()
        self.make_explore_plan()
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.wait_status("AWAITING_NAVIGATOR")
        prewalk_builder.queue(self.run, "execute", "go")
        self.wait_status("READY_FOR_REVIEW")
        self.assertEqual(self.read_turns(log), [1, 2])

    def test_explore_is_not_idle_while_exploring(self):
        os.environ["PREWALK_MOCK_SLOW"] = "3"
        self.make_explore_plan()
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        deadline = time.monotonic() + 10
        status = ""
        while time.monotonic() < deadline:
            status = prewalk_builder.status(self.run)["status"]
            if status == "EXPLORING":
                break
            time.sleep(0.05)
        self.assertEqual(status, "EXPLORING")
        result = prewalk_builder.wait(self.run, 0.5)
        self.assertFalse(result["idle"])
        self.assertEqual(result["status"], "EXPLORING")
        self.wait_status("AWAITING_NAVIGATOR", timeout=15)
        self.execute_and_wait_ready()

    def test_builder_note_cleared_when_reply_has_no_note(self):
        class StubBackend:
            def __init__(self, text):
                self.text = text
            def prompt(self, text, cancel_check=None):
                return {"result": {"stopReason": "end_turn"}, "text": self.text}

        self.make_plan()
        state = {"turn_count": 0, "plan_sent": False, "builder_note": "stale"}
        plan = prewalk_plan.load_plan(self.run / "plan.json")
        reply = prewalk_builder._send_prompt(StubBackend("done" + chr(10) + "NOTE: first note"),
                                             self.run, state, plan, "EXECUTE")
        self.assertEqual(state["builder_note"], "first note")
        prewalk_builder._send_prompt(StubBackend("plain reply, no note"),
                                     self.run, state, plan, "EXECUTE")
        self.assertNotIn("builder_note", state)

    def test_record_usage_model_only(self):
        self.make_plan()
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        self.wait_status("AWAITING_NAVIGATOR")
        prewalk_builder.record_usage(self.run, "navigator", model="nav/m")
        nav = json.loads((self.run / "usage.json").read_text())["navigator"]
        self.assertEqual(nav["models"], ["nav/m"])
        self.assertEqual(nav["totals"], {})
        self.assertEqual(nav.get("call_count", 0), 0)
        with self.assertRaises(ValueError):
            prewalk_builder.record_usage(self.run, "navigator")

    def test_approve_wait_returns_summary_with_savings(self):
        self.make_plan()
        prewalk_builder.launch(self.run, self.run / "plan.json", "mock")
        result = self.cli("execute", "--run-dir", str(self.run), "--text", "go",
                          "--wait", "--timeout", "60")
        self.assertEqual(result["changed_files"], 1)
        self.assertEqual(result["changed_paths"], ["hello.txt"])
        result = self.cli("approve", "--run-dir", str(self.run), "--wait", "--timeout", "60")
        self.assertEqual(result["status"], "approved")
        self.assertTrue(result["idle"])
        self.assertIn("savings", result)
        self.assertEqual(result["savings"]["mode"], "unpriced")


if __name__ == "__main__":
    unittest.main()
