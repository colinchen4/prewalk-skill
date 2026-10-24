from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "codex_deepseek.py"
SPEC = importlib.util.spec_from_file_location("codex_deepseek_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MANAGER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MANAGER
SPEC.loader.exec_module(MANAGER)


class DeepSeekManagerTests(unittest.TestCase):
    def test_agent_defaults_to_max_reasoning(self):
        self.assertEqual(MANAGER.EFFORT, "max")
        self.assertIn('model_reasoning_effort = "max"', MANAGER.expected_agent_text())

    def test_codex_child_env_removes_codex_api_key_case_insensitively(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = MANAGER.resolve_paths(temp_dir)
            with mock.patch.dict(
                os.environ,
                {"CODEX_API_KEY": "do-not-forward", "SAFE_TEST_VALUE": "kept"},
                clear=True,
            ):
                env = MANAGER.codex_child_env(paths)
        self.assertNotIn("CODEX_API_KEY", env)
        self.assertEqual(env["SAFE_TEST_VALUE"], "kept")

    def test_stage_windows_store_runtime_copies_execution_bundle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            resources = root / "OpenAI.Codex_1.2.3_x64__test" / "app" / "resources"
            resources.mkdir(parents=True)
            (resources / "codex.exe").write_bytes(b"codex")
            (resources / MANAGER.WINDOWS_CODE_MODE_HOST).write_bytes(b"host")
            (resources / MANAGER.WINDOWS_SANDBOX_SETUP).write_bytes(b"sandbox-setup")
            (resources / MANAGER.WINDOWS_COMMAND_RUNNER).write_bytes(b"runner")
            paths = MANAGER.resolve_paths(str(root / "home"))
            staged = MANAGER.stage_windows_store_runtime(resources / "codex.exe", paths)
            self.assertEqual(staged.read_bytes(), b"codex")
            self.assertEqual(
                staged.with_name(MANAGER.WINDOWS_CODE_MODE_HOST).read_bytes(),
                b"host",
            )
            self.assertEqual(
                staged.with_name(MANAGER.WINDOWS_SANDBOX_SETUP).read_bytes(),
                b"sandbox-setup",
            )
            self.assertEqual(
                staged.with_name(MANAGER.WINDOWS_COMMAND_RUNNER).read_bytes(),
                b"runner",
            )

    def test_stage_windows_store_runtime_requires_full_bundle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            resources = root / "OpenAI.Codex_1.2.3_x64__test" / "app" / "resources"
            resources.mkdir(parents=True)
            (resources / "codex.exe").write_bytes(b"codex")
            (resources / MANAGER.WINDOWS_CODE_MODE_HOST).write_bytes(b"host")
            (resources / MANAGER.WINDOWS_SANDBOX_SETUP).write_bytes(b"sandbox-setup")
            paths = MANAGER.resolve_paths(str(root / "home"))
            with self.assertRaises(MANAGER.ManagerError):
                MANAGER.stage_windows_store_runtime(resources / "codex.exe", paths)

    def test_executor_workchain_accepts_probe_file_and_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = MANAGER.resolve_paths(str(Path(temp_dir) / "home"))

            def fake_run(*args, **kwargs):
                command = args[0]
                workspace = Path(command[command.index("-C") + 1])
                (workspace / "prewalk_executor_probe.txt").write_text(
                    "EXECUTOR_PROBE_OK\n", encoding="utf-8"
                )
                return SimpleNamespace(
                    returncode=0,
                    stdout='{"type":"turn.completed"}\nWORKCHAIN_OK\n',
                    stderr="",
                )

            with mock.patch.object(MANAGER.subprocess, "run", side_effect=fake_run):
                outcome = MANAGER.executor_workchain_test(paths, "codex.exe")
            self.assertTrue(outcome["executor_workchain"])
            self.assertEqual(outcome["sandbox"], "workspace-write")

    def test_executor_workchain_rejects_missing_probe_file(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout="WORKCHAIN_OK\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = MANAGER.resolve_paths(str(Path(temp_dir) / "home"))
            with mock.patch.object(MANAGER.subprocess, "run", return_value=completed):
                with self.assertRaises(MANAGER.ManagerError):
                    MANAGER.executor_workchain_test(paths, "codex.exe")

    def test_native_test_accepts_executor_surface_pass_phrase(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout='{"type":"turn.completed"}\nagent message: NATIVE_DEEPSEEK_OK\n',
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = MANAGER.resolve_paths(temp_dir)
            with mock.patch.object(MANAGER.subprocess, "run", return_value=completed):
                result = MANAGER.native_test(paths, "codex.exe")
        self.assertTrue(result["desktop_fresh_session_native"])
        self.assertEqual(result["executor_surface"], "codex_exec")
        self.assertEqual(result["model_provider"], "deepseek")
        self.assertEqual(result["model"], "deepseek-flash")
        self.assertEqual(result["reasoning_effort"], "max")
        self.assertEqual(result["agent_role"], "DeepSeek")

    def test_native_test_rejects_missing_pass_phrase(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout="agent message: something else\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = MANAGER.resolve_paths(temp_dir)
            with mock.patch.object(MANAGER.subprocess, "run", return_value=completed):
                with self.assertRaises(MANAGER.ManagerError):
                    MANAGER.native_test(paths, "codex.exe")

    def test_normalizer_does_not_accept_other_text_changes(self):
        self.assertEqual(
            MANAGER.normalize_native_verification_message("NATIVE_DEEPSEEK_OK。"),
            "NATIVE_DEEPSEEK_OK",
        )
        self.assertNotEqual(
            MANAGER.normalize_native_verification_message("NATIVE_DEEPSEEK_OK extra."),
            "NATIVE_DEEPSEEK_OK",
        )

    def test_base_url_default_and_custom_forms_are_canonical(self):
        self.assertEqual(
            MANAGER.canonical_base_url("https://api.deepseek.com"),
            "https://api.deepseek.com/",
        )
        self.assertEqual(
            MANAGER.canonical_base_url(" https://gateway.example.com/v1 "),
            "https://gateway.example.com/v1/",
        )
        self.assertEqual(
            MANAGER.canonical_base_url("http://127.0.0.1:8080"),
            "http://127.0.0.1:8080/",
        )
        self.assertEqual(
            MANAGER.canonical_base_url("http://localhost:8080/v1"),
            "http://localhost:8080/v1/",
        )

    def test_base_url_rejects_unsafe_or_malformed_values(self):
        for value in (
            "",
            "ftp://api.deepseek.com",
            "https://user:secret@api.deepseek.com",
            "https://api.deepseek.com/v1?debug=1",
            "https://api.deepseek.com/v1#fragment",
            "http://api.deepseek.com",
            "https:///missing-host",
            "https://api.deepseek.com /v1",
            "https://api.deepseek.com:bad",
            r"https://api.deepseek.com\v1",
        ):
            with self.subTest(value=value):
                with self.assertRaises(MANAGER.ManagerError):
                    MANAGER.canonical_base_url(value)

    def test_managed_provider_block_renders_custom_base_url(self):
        block = MANAGER.managed_provider_block("https://gateway.example.com/v1/")
        self.assertIn('base_url = "https://gateway.example.com/v1/"', block)

    def test_provider_conflicts_accepts_trailing_slash_equivalent(self):
        provider = {
            "name": "DeepSeek",
            "base_url": "https://api.deepseek.com/",
            "wire_api": "responses",
            "auth": MANAGER.expected_provider_auth(),
        }
        self.assertEqual(
            MANAGER.provider_conflicts(provider, MANAGER.DEFAULT_BASE_URL),
            [],
        )
        self.assertIn(
            "model_providers.deepseek.base_url",
            MANAGER.provider_conflicts(provider, "https://gateway.example.com"),
        )

    def test_status_base_url_check_is_structural(self):
        self.assertIn(
            "model_providers.deepseek.base_url",
            MANAGER.provider_conflicts(
                {
                    "name": "DeepSeek",
                    "base_url": "http://public.example.com",
                    "wire_api": "responses",
                    "auth": MANAGER.expected_provider_auth(),
                }
            ),
        )

    def test_resolve_desired_base_url_preserves_valid_existing(self):
        parsed = {
            "model_providers": {
                "deepseek": {"base_url": "https://gateway.example.com/v1/"}
            }
        }
        self.assertEqual(
            MANAGER.resolve_desired_base_url(parsed, None),
            "https://gateway.example.com/v1/",
        )
        self.assertEqual(
            MANAGER.resolve_desired_base_url(parsed, "https://new.example.com"),
            "https://new.example.com/",
        )
        self.assertEqual(
            MANAGER.resolve_desired_base_url({}, None),
            MANAGER.DEFAULT_BASE_URL,
        )
        self.assertEqual(
            MANAGER.resolve_desired_base_url(
                {"model_providers": {"deepseek": {"base_url": "ftp://bad"}}},
                None,
            ),
            MANAGER.DEFAULT_BASE_URL,
        )

    def test_compatible_existing_accepts_legacy_trailing_slash_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = MANAGER.resolve_paths(temp_dir)
            parsed = {
                "model_providers": {
                    "deepseek": {
                        "name": "DeepSeek",
                        "base_url": "https://api.deepseek.com/",
                        "wire_api": "responses",
                        "auth": MANAGER.expected_provider_auth(),
                    }
                }
            }
            compatible, issues = MANAGER.compatible_existing(
                parsed,
                paths,
                MANAGER.DEFAULT_BASE_URL,
            )
        self.assertTrue(compatible, issues)


if __name__ == "__main__":
    unittest.main()
