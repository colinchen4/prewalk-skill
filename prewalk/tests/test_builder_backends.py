from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
import builder_backends as bb  # noqa: E402
from fakebins import isolated_env, make_bin_dir  # noqa: E402

WT, BRIEF, RUN = "/w/tree", "/w/run/brief.txt", "/w/run"


def snap(launch: bb.Launch) -> list[str]:
    """argv with the resolved executable reduced to its basename."""
    return [os.path.splitext(os.path.basename(launch.argv[0]))[0]] + launch.argv[1:]


class FakePathCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = make_bin_dir(self.root)
        self.env = isolated_env(self.root, self.bin)
        self.patch = unittest.mock.patch.dict(os.environ, self.env, clear=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()


class RegistryTests(unittest.TestCase):
    def test_registry_ids_and_aliases(self):
        ids = [b.id for b in bb.listed()]
        self.assertEqual(ids, ["claude-code-sub", "codex-sub", "kimi-sub", "opencode-api", "pi-api",
                               "codex-deepseek"])
        self.assertIs(bb.get("codex:deepseek"), bb.REGISTRY["codex-deepseek"])
        self.assertEqual(bb.get("mock").id, "mock")
        with self.assertRaises(bb.BackendError):
            bb.get("nope")

    def test_runtime_hosts_cover_every_backend(self):
        for b in bb.REGISTRY.values():
            self.assertIn(b.runtime_host, bb.RUNTIME_HOSTS)

    def test_stubs_refuse_launch(self):
        for bid in ("opencode-api", "pi-api"):
            with self.assertRaises(bb.BackendNotImplemented):
                bb.get(bid).launch(turn=1, workdir=WT, brief_path=BRIEF)


class ClaudeTests(FakePathCase):
    def test_turn1_argv_snapshot(self):
        b = bb.get("claude-code-sub")
        l = b.launch(turn=1, workdir=WT, brief_path=BRIEF, session="11111111-2222-3333-4444-555555555555",
                     writable_dirs=[RUN], allowed_bash=["python3 -m unittest", "rm -rf /; echo"])
        self.assertEqual(snap(l), [
            "claude", "-p", bb.BOOTSTRAP.format(brief=BRIEF), "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits", "--allowedTools",
            "Read", "Edit", "Write", "Glob", "Grep", "TodoWrite",
            "Bash(git status *)", "Bash(git diff *)", "Bash(git log *)", "Bash(python3 -m unittest *)",
            "--permission-prompts", "none", "--add-dir", RUN,
            "--model", "sonnet", "--session-id", "11111111-2222-3333-4444-555555555555",
            "--effort", "medium"])
        self.assertNotIn("Bash", l.argv)                 # never blanket Bash
        self.assertEqual(l.cwd, WT)
        self.assertIsNone(l.stdin_path)
        self.assertEqual((l.requested_effort, l.effective_effort), ("medium", "medium"))

    def test_resume_repeats_model(self):
        l = bb.get("claude-code-sub").launch(turn=2, workdir=WT, brief_path=BRIEF, model="haiku",
                                             effort="low", session="sid")
        argv = snap(l)
        self.assertEqual(argv[argv.index("--resume"):], ["--resume", "sid", "--model", "haiku", "--effort", "low"])
        self.assertIn(bb.BOOTSTRAP_RESUME.format(brief=BRIEF), argv)
        with self.assertRaises(bb.BackendError):
            bb.get("claude-code-sub").launch(turn=2, workdir=WT, brief_path=BRIEF)

    def test_blocked_models(self):
        b = bb.get("claude-code-sub")
        for m in ("best", "fable", "opus", "opus[1m]", "Opus", "opusplan", "default", "claude-opus-5-5"):
            with self.subTest(m=m), self.assertRaises(bb.BackendError):
                b.launch(turn=1, workdir=WT, brief_path=BRIEF, model=m)
        for m in ("sonnet", "haiku", "claude-sonnet-5-5"):
            b.check_model(m)

    def test_env_strips_api_and_gateway_vars(self):
        l = bb.get("claude-code-sub").launch(turn=1, workdir=WT, brief_path=BRIEF)
        base = {"PATH": "/bin", "ANTHROPIC_API_KEY": "SENTINEL-SECRET", "ANTHROPIC_AUTH_TOKEN": "SENTINEL-SECRET",
                "ANTHROPIC_BASE_URL": "http://gw", "ANTHROPIC_MODEL": "opus",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "opus", "CLAUDE_CODE_EFFORT_LEVEL": "max",
                "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1", "OPENAI_API_KEY": "keep"}
        env = l.build_env(base)
        self.assertEqual(env, {"PATH": "/bin", "OPENAI_API_KEY": "keep"})
        self.assertNotIn("SENTINEL-SECRET", json.dumps(l.describe()))   # describe never carries env values

    def test_env_removal_is_case_insensitive_on_windows(self):
        launch = bb.get("claude-code-sub").launch(turn=1, workdir=WT, brief_path=BRIEF)
        with unittest.mock.patch("builder_backends.base.os.name", "nt"):
            env = launch.build_env({"anthropic_api_key": "secret",
                                    "Anthropic_Default_Sonnet_Model": "opus", "PATH": "/bin"})
        self.assertEqual(env, {"PATH": "/bin"})

    def test_auth_probe_subscription_and_logged_out(self):
        b = bb.get("claude-code-sub")
        d = b.detect()
        self.assertTrue(d.installed)
        self.assertIn("2.1.310", d.version)
        self.assertEqual((b.auth_check(d).state, b.auth_check(d).billing), ("ready", "subscription"))
        os.environ["FAKE_CLAUDE_AUTH"] = "api_key"
        self.assertEqual(b.auth_check(d).billing, "api_key")
        os.environ["FAKE_CLAUDE_AUTH"] = "logged_out"
        self.assertEqual(b.auth_check(d).state, "needs_login")
        log = Path(os.environ["FAKE_LOG"]).read_text()
        self.assertEqual(set(l.split(" ", 1)[1] for l in log.splitlines()), {"--version", "auth status"})

    def test_auth_probe_runs_with_stripped_env(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        self.assertNotIn("ANTHROPIC_API_KEY", bb.get("claude-code-sub").probe_env())

    def test_old_version_warns(self):
        os.environ["FAKE_CLAUDE_VERSION"] = "2.1.200"
        row = bb.get("claude-code-sub").describe()
        self.assertTrue(any("older than required 2.1.302" in n for n in row["notes"]))

    def test_observed_models_from_stream(self):
        p = Path(self.tmp.name) / "ev.jsonl"
        p.write_text("\n".join(json.dumps(e) for e in (
            {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5", "session_id": "s"},
            {"type": "result", "session_id": "s", "modelUsage": {"claude-sonnet-5-5": {}, "claude-haiku-5-5": {}}},
        )))
        self.assertEqual(bb.get("claude-code-sub").observed_models(p), ["claude-haiku-5-5", "claude-sonnet-5-5"])


class CodexTests(FakePathCase):
    def test_turn1_argv_snapshot(self):
        l = bb.get("codex-sub").launch(turn=1, workdir=WT, brief_path=BRIEF, writable_dirs=[RUN])
        self.assertEqual(snap(l), ["codex", "exec", "--json", "-s", "workspace-write", "-C", WT,
                                   "-m", "gpt-6-luna", "--add-dir", RUN,
                                   "-c", 'model_reasoning_effort="high"', "-"])
        self.assertEqual(l.stdin_path, BRIEF)
        self.assertEqual(l.cwd, WT)

    def test_resume_argv_snapshot(self):
        l = bb.get("codex-sub").launch(turn=3, workdir=WT, brief_path=BRIEF, model="gpt-6.1-terra",
                                       effort="medium", session="thread-9")
        self.assertEqual(snap(l), ["codex", "exec", "resume", "--json", "thread-9", "-m", "gpt-6.1-terra",
                                   "-c", 'model_reasoning_effort="medium"', "-"])

    def test_env_and_blocked(self):
        l = bb.get("codex-sub").launch(turn=1, workdir=WT, brief_path=BRIEF)
        env = l.build_env({"OPENAI_API_KEY": "k", "CODEX_API_KEY": "k", "CODEX_HOME": "/c", "HOME": "/h"})
        self.assertEqual(env, {"CODEX_HOME": "/c", "HOME": "/h"})
        for m in ("gpt-6.1-sol", "gpt-6-sol", "astra"):
            with self.subTest(m=m), self.assertRaises(bb.BackendError):
                bb.get("codex-sub").check_model(m)

    def test_effort_below_minimum_is_rejected(self):
        with self.assertRaises(bb.BackendError):
            bb.get("codex-sub").launch(turn=1, workdir=WT, brief_path=BRIEF, effort="minimal")

    def test_auth_probe(self):
        b = bb.get("codex-sub")
        self.assertEqual((b.auth_check().state, b.auth_check().billing), ("ready", "subscription"))
        os.environ["FAKE_CODEX_AUTH"] = "logged_out"
        self.assertEqual(b.auth_check().state, "needs_login")


class KimiAndLegacyTests(FakePathCase):
    def test_kimi_argv(self):
        b = bb.get("kimi-sub")
        l1 = b.launch(turn=1, workdir=WT, brief_path=BRIEF, effort="high")
        self.assertEqual(snap(l1), ["kimi", "-m", "kimi-code/kimi-for-coding", "-p",
                                    bb.BOOTSTRAP.format(brief=BRIEF), "--output-format", "stream-json"])
        self.assertEqual((l1.requested_effort, l1.effective_effort), ("high", None))
        l2 = b.launch(turn=2, workdir=WT, brief_path=BRIEF, session="k-1")
        self.assertEqual(snap(l2)[:3], ["kimi", "--session", "k-1"])
        self.assertEqual(b.auth_check().state, "needs_login")

    def test_kimi_exit_75_is_rate_limit(self):
        self.assertEqual(bb.get("kimi-sub").classify_failure(75, ""), "rate_limited")

    def test_legacy_hidden_without_manager_and_not_in_chain(self):
        b = bb.get("codex-deepseek")
        self.assertFalse(b.detect().installed)
        self.assertFalse(b.in_default_chain)


class EffortAndLimitTests(unittest.TestCase):
    def test_resolve_effort_never_rounds_up(self):
        b = bb.get("claude-code-sub")
        with self.assertRaises(bb.BackendError):
            b.resolve_effort("minimal")
        self.assertEqual(b.resolve_effort("xhigh"), "xhigh")
        self.assertIsNone(b.resolve_effort(None))
        with self.assertRaises(bb.BackendError):
            b.resolve_effort("ultra")
        self.assertEqual(bb.get("mock").resolve_effort("max"), "high")

    def test_rate_limit_classification(self):
        b = bb.get("claude-code-sub")
        for text in ("You've hit your limit · resets 5pm", "Error: 429 Too Many Requests",
                     "rate_limit_exceeded", "Usage limit reached for Sonnet"):
            with self.subTest(text=text):
                self.assertEqual(b.classify_failure(1, text), "rate_limited")
        self.assertIsNone(b.classify_failure(1, "SyntaxError: invalid syntax"))
        self.assertIsNone(b.classify_failure(0, "You've hit your limit"))


class RecommendTests(unittest.TestCase):
    def rows(self, ready=("claude-code-sub", "codex-sub")):
        out = []
        for b in bb.listed():
            r = {"id": b.id, "track": b.track, "family": b.family, "implemented": b.implemented,
                 "in_default_chain": b.in_default_chain, "installed": b.id in ready,
                 "auth": "ready" if b.id in ready else "not_installed", "billing": "subscription",
                 "weak_model": b.weak_model, "weak_effort": b.weak_effort}
            out.append(r)
        return out

    def test_same_family_first(self):
        self.assertEqual(bb.recommend(self.rows(), "codex")["backend"], "codex-sub")
        rec = bb.recommend(self.rows(), "claude-code")
        self.assertEqual((rec["backend"], rec["model"], rec["effort"]), ("claude-code-sub", "sonnet", "medium"))

    def test_cross_family_fallback_in_registry_order(self):
        self.assertEqual(bb.recommend(self.rows(("codex-sub", "kimi-sub")), "claude-code")["backend"], "codex-sub")
        self.assertEqual(bb.recommend(self.rows(("kimi-sub",)), None)["backend"], "kimi-sub")

    def test_saved_ready_wins_and_stale_saved_ignored(self):
        saved = {"backend": "codex-sub", "model": "gpt-6.1-terra", "effort": "medium"}
        rec = bb.recommend(self.rows(), "claude-code", saved)
        self.assertEqual((rec["backend"], rec["model"], rec["reason"]), ("codex-sub", "gpt-6.1-terra", "saved config"))
        self.assertEqual(bb.recommend(self.rows(("claude-code-sub",)), "claude-code", saved)["backend"],
                         "claude-code-sub")

    def test_stubs_and_legacy_never_recommended(self):
        self.assertIsNone(bb.recommend(self.rows(("opencode-api", "pi-api", "codex-deepseek")), "codex"))

    def test_track_a_api_key_login_is_not_usable(self):
        rows = self.rows(("claude-code-sub",))
        rows[0]["billing"] = "api_key"
        self.assertIsNone(bb.recommend(rows, "claude-code"))
        saved = {"backend": "claude-code-sub", "model": "sonnet", "effort": "medium"}
        self.assertIsNone(bb.recommend(rows, "claude-code", saved))


if __name__ == "__main__":
    unittest.main()
