from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=ResourceWarning, message="subprocess")
sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
import prewalk  # noqa: E402
import prewalk_config  # noqa: E402
from fakebins import isolated_env, make_bin_dir  # noqa: E402


class CliCase(unittest.TestCase):
    bins = ("claude", "codex", "kimi")

    def setUp(self):
        warnings.simplefilter("ignore", ResourceWarning)  # detached supervisor
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = make_bin_dir(self.root, self.bins) if self.bins else None
        self.env = isolated_env(self.root, self.bin)
        self.patch = unittest.mock.patch.dict(os.environ, self.env, clear=True)
        self.patch.start()
        self.home = Path(self.env["PREWALK_HOME"])

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = prewalk.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class ModelsTests(CliCase):
    def test_models_lists_ready_choices_without_writing_config(self):
        os.environ["FAKE_KIMI_AUTH"] = "oauth"
        code, out, _ = self.cli("models", "--json", "--navigator-host", "kimi")
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertFalse(self.home.exists())
        kimi = next(row for row in report["backends"] if row["backend"] == "kimi-sub")
        self.assertEqual(kimi["models"], ["kimi-code/kimi-for-coding", "kimi-code/k3-256k"])
        self.assertEqual(kimi["default_model"], "kimi-code/kimi-for-coding")
        self.assertIsNone(kimi["default_effort"])

    def test_models_text_output(self):
        code, out, _ = self.cli("models", "--navigator-host", "claude-code")
        self.assertEqual(code, 0)
        self.assertIn("claude-code-sub: models=sonnet, haiku", out)
        self.assertIn("default=sonnet/medium", out)


class DoctorTests(CliCase):
    def test_same_family_recommendation_json(self):
        code, out, _ = self.cli("doctor", "--json", "--navigator-host", "codex")
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(report["recommended"]["backend"], "codex-sub")
        self.assertEqual(report["recommended"]["model"], "gpt-6-luna")
        self.assertEqual(report["recommended"]["effort"], "high")
        self.assertTrue(any("shares your codex plan quota" in n for n in report["notes"]))
        self.assertNotIn("mock", [r["id"] for r in report["backends"]])

    def test_host_from_env_and_cross_family(self):
        os.environ["PREWALK_HOST"] = "kimi"   # no managed OAuth provider -> cross-family fallback
        code, out, _ = self.cli("doctor", "--json")
        report = json.loads(out)
        self.assertEqual(report["navigator_host_source"], "PREWALK_HOST")
        self.assertEqual(report["recommended"]["backend"], "claude-code-sub")

    def test_kimi_desktop_oauth_is_recommended(self):
        os.environ["FAKE_KIMI_AUTH"] = "oauth"
        code, out, _ = self.cli("doctor", "--json", "--navigator-host", "kimi")
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(report["recommended"]["backend"], "kimi-sub")
        self.assertEqual(report["recommended"]["model"], "kimi-code/kimi-for-coding")

    def test_api_key_logins_are_not_auto_recommended(self):
        os.environ["FAKE_CLAUDE_AUTH"] = "api_key"
        os.environ["FAKE_CODEX_AUTH"] = "api_key"
        code, out, _ = self.cli("doctor", "--json", "--navigator-host", "claude-code")
        self.assertEqual(code, 3)
        self.assertIsNone(json.loads(out)["recommended"])

    def test_doctor_is_read_only(self):
        self.cli("doctor", "--navigator-host", "claude-code")
        self.assertFalse(self.home.exists())
        log = Path(os.environ["FAKE_LOG"]).read_text().splitlines()
        self.assertTrue(set(l.split(" ", 1)[1] for l in log) <= {"--version", "auth status", "login status", "provider list"})

    def test_text_output_has_accept_line(self):
        code, out, _ = self.cli("doctor", "--navigator-host", "claude-code")
        self.assertIn("Recommended Builder: claude-code-sub  model=sonnet  effort=medium", out)
        self.assertIn("setup --backend claude-code-sub --model sonnet --effort medium", out)


class NothingReadyTests(CliCase):
    bins = ()

    def test_fallback_exit_3(self):
        code, out, _ = self.cli("doctor", "--navigator-host", "codex")
        self.assertEqual(code, 3)
        self.assertIn("claude auth login", out)
        self.assertIn("DEEPSEEK_API_KEY", out)
        self.assertIn("Then run: prewalk doctor", out)
        code, out, _ = self.cli("doctor", "--json")
        self.assertEqual((code, json.loads(out)["recommended"]), (3, None))

    def test_models_empty_is_informational_not_a_gate(self):
        code, out, _ = self.cli("models", "--json", "--navigator-host", "kimi")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["backends"], [])


class LoggedOutTests(CliCase):
    def test_logged_out_falls_back(self):
        os.environ["FAKE_CODEX_AUTH"] = "logged_out"
        code, out, _ = self.cli("doctor", "--json", "--navigator-host", "codex")
        report = json.loads(out)
        self.assertEqual(report["recommended"]["backend"], "claude-code-sub")
        os.environ["FAKE_CLAUDE_AUTH"] = "logged_out"
        code, _, _ = self.cli("doctor", "--json", "--navigator-host", "codex")
        self.assertEqual(code, 3)


class SetupTests(CliCase):
    def test_setup_accepts_recommendation_and_saved_wins(self):
        code, out, _ = self.cli("setup", "--navigator-host", "claude-code", "--json")
        self.assertEqual(code, 0)
        cfg = json.loads(prewalk_config.config_path().read_text())
        self.assertEqual(cfg["builder"], {"backend": "claude-code-sub", "model": "sonnet",
                                          "effort": "medium", "max_concurrency": 1})
        self.assertEqual(cfg["schema_version"], 2)
        if os.name != "nt":
            self.assertEqual(oct(prewalk_config.config_path().stat().st_mode & 0o777), "0o600")
        code, out, _ = self.cli("doctor", "--json", "--navigator-host", "codex")
        self.assertEqual(json.loads(out)["recommended"]["reason"], "saved config")
        self.assertEqual(json.loads(out)["recommended"]["backend"], "claude-code-sub")

    def test_setup_migrates_last_pairing(self):
        self.home.mkdir(parents=True)
        legacy = self.home / "last-pairing.json"
        legacy.write_text(json.dumps({"navigator_model": "opus", "navigator_effort": "high",
                                      "builder_model": "sonnet", "builder_effort": "medium"}))
        _, out, _ = self.cli("doctor", "--json", "--navigator-host", "claude-code")
        self.assertEqual(json.loads(out)["config_source"], "migrated")
        code, out, _ = self.cli("setup", "--backend", "codex-sub", "--model", "gpt-6.1-terra", "--json")
        result = json.loads(out)
        self.assertTrue(result["migrated"])
        cfg = json.loads(prewalk_config.config_path().read_text())
        self.assertEqual(cfg["pairing"]["builder_model"], "sonnet")
        self.assertEqual(cfg["builder"]["model"], "gpt-6.1-terra")
        self.assertEqual(cfg["builder"]["effort"], "high")
        self.assertTrue(legacy.exists())          # older skill versions keep working

    def test_setup_rejects_blocked_model_and_stub(self):
        code, _, err = self.cli("setup", "--backend", "claude-code-sub", "--model", "opus")
        self.assertEqual(code, 2)
        self.assertIn("not allowed as Builder", err)
        code, _, err = self.cli("setup", "--backend", "pi-api")
        self.assertEqual(code, 2)
        self.assertFalse(prewalk_config.config_path().exists())

    def test_config_never_holds_secrets(self):
        with self.assertRaises(prewalk_config.ConfigError):
            prewalk_config.save({"builder": {"backend": "codex-sub", "api_key": "sk-1"}})
        with self.assertRaises(prewalk_config.ConfigError):
            prewalk_config.save({"builder": {"backend": "codex-sub"}, "oauth_token": "t"})
        with self.assertRaises(prewalk_config.ConfigError):
            prewalk_config.save({"builder": {"backend": "codex-sub", "max_concurrency": 3}})


class SelfTestTests(CliCase):
    def test_mock_round_trip(self):
        code, out, _ = self.cli("test", "--backend", "mock", "--timeout", "60")
        result = json.loads(out)
        self.assertEqual(code, 0, out)
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(result["observed_models"], ["mock-ok"])
        self.assertIsNone(result["workdir"])

    def test_rate_limited_round_trip_is_reported(self):
        code, out, _ = self.cli("test", "--backend", "mock", "--model", "mock-rate-limit", "--timeout", "60")
        result = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "waiting_rate_limit")
        self.assertIn("hit your limit", result["error"])

    def test_no_backend_configured(self):
        code, out, _ = self.cli("test")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
