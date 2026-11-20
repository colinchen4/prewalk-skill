from __future__ import annotations

import contextlib
import io
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
sys.path.insert(0, str(Path(__file__).parent))
import builder_backends as bb  # noqa: E402
from builder_backends import devin_sub  # noqa: E402
import persistent_backends  # noqa: E402
import prewalk  # noqa: E402
import prewalk_builder  # noqa: E402
import prewalk_config  # noqa: E402
import prewalk_plan  # noqa: E402
import prewalk_runtime  # noqa: E402
import prewalk_savings  # noqa: E402
from fakebins import isolated_env, make_bin_dir  # noqa: E402

FAKE_DEVIN_ACP = r'''
import json
import os
import sys
from pathlib import Path

MODEL_OPTIONS = [
    {"value": "swe-2-high", "name": "SWE-2"},
    {"value": "swe-2-flash-medium", "name": "SWE-2 Flash"},
]
LABELS = {
    ("swe-2-high", "medium"): "SWE-2 Medium",
    ("swe-2-high", "high"): "SWE-2 High",
    ("swe-2-high", "max"): "SWE-2 Max",
    ("swe-2-flash-medium", "medium"): "SWE-2 Flash Medium",
    ("swe-2-flash-medium", "high"): "SWE-2 Flash High",
}

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def config_options(mode, model, thought):
    return [
        {"id": "mode", "currentValue": mode, "options": [
            {"value": "accept-edits", "name": "Code"},
            {"value": "bypass", "name": "Bypass Permissions"},
        ]},
        {"id": "model", "currentValue": model, "options": MODEL_OPTIONS},
        {"id": "thought_level", "currentValue": thought, "options": [
            {"value": "medium", "name": "Medium"},
            {"value": "high", "name": "High"},
            {"value": "max", "name": "Max"},
        ]},
    ]

mode = "accept-edits"
model = "fusion-claude-opus-5-5-xhigh-sidekick-swe-2-high"
thought = "high"
session_id = "fake-devin-session-1"
turn = 0

if os.environ.get("ACP_BACKEND"):
    sys.stderr.write("ACP_BACKEND leaked into devin acp\n")
    raise SystemExit(2)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    req_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        emit({"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": 1}})
    elif method == "session/new":
        emit({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": session_id,
            "update": {"sessionUpdate": "config_option_update",
                       "configOptions": config_options(mode, model, thought)},
        }})
        emit({"jsonrpc": "2.0", "id": req_id, "result": {
            "sessionId": session_id,
            "configOptions": config_options(mode, model, thought),
        }})
    elif method == "session/set_config_option":
        cid = params.get("configId")
        val = params.get("value")
        if cid == "model":
            if val not in ("swe-2-high", "swe-2-flash-medium"):
                emit({"jsonrpc": "2.0", "id": req_id,
                      "error": {"code": -32602, "message": f"Invalid value {val!r}"}})
                continue
            model = val
            thought = "high" if val == "swe-2-high" else "medium"
        elif cid == "thought_level":
            thought = val
        elif cid == "mode":
            mode = val
        emit({"jsonrpc": "2.0", "id": req_id, "result": {
            "configOptions": config_options(mode, model, thought),
        }})
    elif method == "session/set_mode":
        mode = params.get("modeId") or mode
        emit({"jsonrpc": "2.0", "id": req_id, "result": None})
    elif method == "session/prompt":
        turn += 1
        target = Path.cwd() / "hello.txt"
        content = target.read_text(encoding="utf-8") if target.exists() else ""
        if turn == 1:
            target.write_text(content + "line1\n", encoding="utf-8")
        else:
            target.write_text(content + "line2\n", encoding="utf-8")
        turn_uid = f"turn-msg-{turn}"
        label = LABELS.get((model, thought), model)
        # Emit two calls per turn, each with a subagent_context duplicate mirror (matching real Devin ACP).
        for call_in, call_out, call_cache in ((1000, 40, 200), (1200, 60, 800)):
            emit({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "usage_update", "used": call_in + call_out, "size": 262144,
                           "_meta": {"cognition.ai/inputTokens": call_in,
                                     "cognition.ai/outputTokens": call_out,
                                     "cognition.ai/cachedReadTokens": call_cache}},
            }})
            emit({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "usage_update", "used": call_in + call_out, "size": 262144,
                           "_meta": {"cognition.ai/inputTokens": call_in,
                                     "cognition.ai/outputTokens": call_out,
                                     "cognition.ai/cachedReadTokens": call_cache,
                                     "cognition.ai/subagent_context": {"parentAgentId": "root"}}},
            }})
        emit({"jsonrpc": "2.0", "method": "_cognition.ai/turn_stats", "params": {
            "sessionId": session_id,
            "turnClientMessageId": turn_uid,
            "responseDimensions": [
                {"uid": "agent_messages", "kind": {"type": "cumulativeMetric", "value": 2.0}},
                {"uid": "model", "kind": {"type": "metric", "value": label}},
                {"uid": "input_tokens", "kind": {"type": "cumulativeMetric", "value": 2200.0}},
                {"uid": "output_tokens", "kind": {"type": "cumulativeMetric", "value": 100.0}},
                {"uid": "cached_input_tokens", "kind": {"type": "cumulativeMetric", "value": 1000.0}},
            ],
        }})
        emit({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": session_id,
            "update": {"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": f"Updated hello.txt on turn {turn}.\nNOTE: ok"}},
        }})
        emit({"jsonrpc": "2.0", "id": req_id, "result": {
            "stopReason": "end_turn",
            "usage": {"totalTokens": 1260, "inputTokens": 1200, "outputTokens": 60, "cachedReadTokens": 800},
            "_meta": {"cognition.ai/userMessageId": turn_uid},
        }})
    elif method == "session/close":
        emit({"jsonrpc": "2.0", "id": req_id, "result": None})
        break
'''


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env=env).stdout


class DevinSubSpecTests(unittest.TestCase):
    def test_registered_and_aliased(self):
        b = bb.get("devin-sub")
        self.assertIs(bb.get("devin"), b)
        self.assertEqual((b.id, b.family, b.runtime_host, b.persistent),
                         ("devin-sub", "devin", "devin", True))
        self.assertEqual((b.weak_model, b.weak_effort), ("swe-2-high", "medium"))
        self.assertEqual(b.alt_models, ("swe-2-flash-medium",))
        self.assertEqual(b.supported_efforts, ("medium", "high", "max"))

    def test_blocked_frontier_models_rejected(self):
        b = bb.get("devin-sub")
        for bad in ("gemini-4-neon-high", "fusion-claude-opus-5-5-xhigh-sidekick-swe-2-high",
                    "swe-2-high-lightning", "claude-opus-4-6", "gemini-4-argon-high"):
            with self.subTest(model=bad):
                with self.assertRaises(bb.BackendError):
                    b.check_model(bad)
        self.assertEqual(b.check_model("swe-2-high"), "swe-2-high")
        self.assertEqual(b.check_model("swe-2-flash-medium"), "swe-2-flash-medium")

    def test_model_normalization_and_matching(self):
        self.assertEqual(devin_sub.split_model_effort("SWE-2 Flash Medium"), ("swe-2-flash", "medium"))
        self.assertEqual(devin_sub.split_model_effort("SWE-2 Medium"), ("swe-2", "medium"))
        self.assertEqual(devin_sub.effective_model_slug("swe-2-high", "medium"), "swe-2-medium")
        self.assertTrue(devin_sub.models_match("swe-2-high", "medium", "SWE-2 Medium"))
        self.assertTrue(devin_sub.models_match("swe-2-high", "medium", "swe-2-high", "medium"))
        self.assertFalse(devin_sub.models_match("swe-2-high", "high", "SWE-2 Medium"))
        self.assertFalse(devin_sub.models_match("swe-2-flash-medium", "medium", "SWE-2 Medium"))

    def test_resolve_acp_model_option_maps_family_and_effort(self):
        opts = [{"value": "swe-2-high"}, {"value": "swe-2-high-lightning"}, {"value": "swe-2-flash-medium"}]
        self.assertEqual(devin_sub.resolve_acp_model_option("swe-2-high", opts, "medium"),
                         ("swe-2-high", "medium"))
        self.assertEqual(devin_sub.resolve_acp_model_option("swe-2-high", opts, None),
                         ("swe-2-high", "high"))
        self.assertEqual(devin_sub.resolve_acp_model_option("swe-2-medium", opts, None),
                         ("swe-2-high", "medium"))
        self.assertEqual(devin_sub.resolve_acp_model_option("swe-2-flash-high", opts, None),
                         ("swe-2-flash-medium", "high"))

    def test_env_sanitization_strips_acp_backend(self):
        b = bb.get("devin-sub")
        env = b.launch(turn=1, workdir="/wt", brief_path="/b.md").build_env({
            "PATH": "/bin", "ACP_BACKEND": "windsurf", "WINDSURF_API_KEY": "secret",
            "DEVIN_MODEL": "gemini-4-neon-high", "KEEP": "1",
        })
        self.assertEqual(env, {"PATH": "/bin", "KEEP": "1"})


class DevinCliDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = make_bin_dir(self.root, ("devin",))
        self.env = isolated_env(self.root, self.bin, FAKE_DEVIN_AUTH="subscription",
                                AI_AGENT="devin_3000-14-30825_agent")
        self.patch = mock.patch.dict(os.environ, self.env, clear=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = prewalk.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_auto_detects_devin_host_and_lists_swe2_models(self):
        code, out, _ = self.cli("models", "--json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["navigator_host"], "devin")
        self.assertEqual(report["navigator_host_source"], "auto (DEVIN env)")
        devin = next(r for r in report["backends"] if r["backend"] == "devin-sub")
        self.assertEqual(devin["models"], ["swe-2-high", "swe-2-flash-medium"])
        self.assertEqual(devin["default_model"], "swe-2-high")
        self.assertEqual(devin["default_effort"], "medium")
        self.assertTrue(devin["persistent"])

    def test_doctor_recommends_devin_sub_and_setup_savings_flag(self):
        code, out, _ = self.cli("doctor", "--json")
        self.assertEqual(code, 0)
        rec = json.loads(out)["recommended"]
        self.assertEqual((rec["backend"], rec["model"], rec["effort"]),
                         ("devin-sub", "swe-2-high", "medium"))
        self.assertFalse(prewalk_savings.is_enabled())
        code, out, _ = self.cli("setup", "--backend", "devin-sub", "--savings", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["config"]["savings"])
        self.assertTrue(prewalk_savings.is_enabled())
        code, out, _ = self.cli("setup", "--no-savings", "--json")
        self.assertEqual(code, 0)
        self.assertFalse(prewalk_savings.is_enabled())


class DevinPersistentAcpTests(unittest.TestCase):
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
        self.fake_script = self.root / "fake_devin_acp.py"
        self.fake_script.write_text(FAKE_DEVIN_ACP, encoding="utf-8")
        if os.name == "nt":
            self.fake_bin = self.root / "devin.cmd"
            self.fake_bin.write_text(
                f'@echo off\r\n"{sys.executable}" "{self.fake_script}" %*\r\n', encoding="utf-8"
            )
        else:
            self.fake_bin = self.root / "devin"
            self.fake_bin.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{self.fake_script}" "$@"\n', encoding="utf-8"
            )
            self.fake_bin.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {
            "PREWALK_HOME": str(self.root / "home"),
            "GIT_CEILING_DIRECTORIES": str(self.root),
            "DEVIN_BIN": str(self.fake_bin),
            "ACP_BACKEND": "windsurf",
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

    def test_persistent_acp_route_usage_and_optional_savings(self):
        check_cmd = f'"{sys.executable}" -c "from pathlib import Path; assert Path(\'hello.txt\').read_text().startswith(\'base\\nline1\\n\')"'
        plan = prewalk_plan.create_plan(
            "devin-h1", "brief_only", "Append lines to hello.txt",
            ["hello.txt updated"], ["hello.txt"], cwd=str(self.repo),
            validation=[check_cmd], model="swe-2-flash-medium", effort="medium",
        )
        prewalk_plan.write_plan(self.run, plan)

        # Blocked Navigator model cannot be launched.
        bad_plan = prewalk_plan.create_plan(
            "devin-bad", "brief_only", "bad", ["ok"], ["hello.txt"], cwd=str(self.repo),
            validation=[check_cmd], model="gemini-4-neon-high",
        )
        bad_path = prewalk_plan.write_plan(self.root / "bad-run", bad_plan)
        with self.assertRaises(bb.BackendError):
            prewalk_builder.launch(self.root / "bad-run", bad_path, "devin-sub")

        prewalk_builder.launch(self.run, self.run / "plan.json", "devin")
        st = self.wait_status("AWAITING_NAVIGATOR")
        self.assertEqual(st["backend"], "devin-sub")
        self.assertEqual(st["model_status"], "verified")
        self.assertEqual(st["config_evidence"]["mode"], "bypass")
        self.assertEqual(st["config_evidence"]["effective_model"], "swe-2-flash-medium")
        pid1, sid1 = st["builder_pid"], st["session_id"]

        # Turn 1 on SWE-2 Flash Medium
        prewalk_builder.queue(self.run, "execute", "first pass")
        st = self.wait_status("READY_FOR_REVIEW")
        self.assertEqual(st["usage_status"], "available")
        self.assertEqual(st["model_status"], "verified")
        self.assertEqual(st["observed_models"], ["swe-2-flash-medium"])

        # Blocked model cannot be queued via route.
        with self.assertRaises(bb.BackendError):
            prewalk_builder.queue(self.run, "route", model="gemini-4-neon-high", compaction_id="c-bad")

        # Route mid-run to SWE-2 High on the SAME PID & session.
        prewalk_builder.queue(self.run, "route", model="swe-2-high", effort="high",
                              reason="escalate mechanical work", compaction_id="compact-1")
        prewalk_builder.queue(self.run, "execute", "second pass")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            st = prewalk_builder.status(self.run)
            if st.get("execution_count") == 2 and st["status"] == "READY_FOR_REVIEW":
                break
            time.sleep(0.05)
        self.assertEqual((st["builder_pid"], st["session_id"]), (pid1, sid1))
        self.assertEqual(st["model_status"], "verified")
        self.assertEqual(st["config_evidence"]["effective_model"], "swe-2-high")
        self.assertEqual(st["observed_models"], ["swe-2-flash-medium", "swe-2-high"])

        usage = json.loads((self.run / "builder-usage.json").read_text(encoding="utf-8"))
        self.assertEqual(usage["call_count"], 4)  # 2 per turn, subagent duplicates skipped
        self.assertEqual(usage["totals"]["input_tokens"], 4400)
        self.assertEqual(usage["totals"]["cached_input_tokens"], 2000)
        self.assertEqual(usage["totals"]["inputOther"], 2400)
        self.assertEqual(usage["totals"]["inputCacheRead"], 2000)
        self.assertEqual(usage["totals"]["output"], 200)
        self.assertEqual([c["model"] for c in usage["calls"]],
                         ["swe-2-flash-medium", "swe-2-high"])

        # Approve with default savings OFF -> no savings.json
        prewalk_builder.queue(self.run, "approve")
        st = self.wait_status("approved")
        self.assertEqual((self.repo / "hello.txt").read_text(encoding="utf-8"), "base\nline1\nline2\n")
        self.assertFalse((self.run / "savings.json").exists())
        self.assertNotIn("savings", prewalk_builder.summary(self.run))

        # On-demand savings report with prices prices each routed model separately.
        prices_path = self.root / "prices.json"
        prices_path.write_text(json.dumps({
            "gemini-4-neon-high": {"input": 10, "cache_read": 1, "cache_write": 2, "output": 30},
            "swe-2-high": {"input": 2, "cache_read": 0.2, "cache_write": 0.4, "output": 6},
            "swe-2-flash-medium": {"input": 0.5, "cache_read": 0.05, "cache_write": 0.1, "output": 1.5},
        }), encoding="utf-8")
        os.environ["PREWALK_PRICES_PATH"] = str(prices_path)
        prewalk_builder.record_usage(self.run, "navigator", model="gemini-4-neon-high")
        report = prewalk_savings.run_report(self.run)
        self.assertEqual(report["mode"], "builder_only")
        self.assertGreater(report["builder_only_savings_pct"], 80.0)


if __name__ == "__main__":
    unittest.main()
