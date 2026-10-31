from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import persistent_backends
import prewalk_builder
import prewalk_runtime


class UsageTests(unittest.TestCase):
    def test_usage_is_per_role_and_never_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            state = {"backend": "kimi-sub", "session_id": "s1", "builder_worktree": "/wt",
                     "model": "kimi-code/kimi-for-coding"}
            payload = {"usage_status": "available", "models": ["kimi-code/kimi-for-coding"],
                       "totals": {"inputOther": 10, "inputCacheRead": 5,
                                  "inputCacheCreation": 2, "output": 3}}
            with mock.patch.object(prewalk_runtime, "_kimi_usage", return_value=payload) as usage:
                prewalk_builder.collect_usage(run, state, 1)
            self.assertEqual(usage.call_args.args[2], 1)
            roles = json.loads((run / "usage.json").read_text())
            self.assertEqual(roles["builder"]["totals"]["inputCacheRead"], 5)
            self.assertEqual(roles["navigator"]["usage_status"], "not_collected")
            self.assertEqual(state["cache_usage"]["inputCacheCreation"], 2)
            self.assertEqual(state["usage_status"], "available")
            self.assertEqual(state["observed_models"], ["kimi-code/kimi-for-coding"])
            with mock.patch.object(prewalk_runtime, "_load_prior_usage", return_value=object()), \
                 mock.patch.object(prewalk_runtime, "_kimi_usage", return_value=payload) as usage:
                prewalk_builder.collect_usage(run, state, 2)
            self.assertEqual(usage.call_args.args[2], 2)


class IntegrityTests(unittest.TestCase):
    def test_environment_is_sanitized_without_losing_desktop_oauth_home(self):
        with mock.patch("persistent_backends.base.os.name", "nt"):
            env = persistent_backends.base.child_env({
                "anthropic_api_key": "secret", "OPENAI_BASE_URL": "https://gateway",
                "DEEPSEEK_API_KEY": "secret", "KIMI_MODEL": "main-model",
                "KIMI_CODE_HOME": "keep-oauth"}, persistent_backends.base.BUILDER_ENV_REMOVE)
        self.assertEqual(env, {"KIMI_CODE_HOME": "keep-oauth"})

    def test_binary_change_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "builder.exe"
            executable.write_text("original", encoding="utf-8")
            backend = object.__new__(persistent_backends.JsonRpcBackend)
            backend.integrity = persistent_backends.binary_integrity(str(executable))
            backend.verify_integrity()
            executable.write_text("changed", encoding="utf-8")
            with self.assertRaises(persistent_backends.PersistentError):
                backend.verify_integrity()


if __name__ == "__main__":
    unittest.main()
