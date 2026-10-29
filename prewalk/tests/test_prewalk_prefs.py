from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "prewalk_prefs.py"
SPEC = importlib.util.spec_from_file_location("prewalk_prefs_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PREFS = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PREFS
SPEC.loader.exec_module(PREFS)

SKILL = Path(__file__).parents[1] / "SKILL.md"


class PrewalkPrefsTests(unittest.TestCase):
    def test_recommended_defaults(self):
        defaults = PREFS.recommended_defaults()
        self.assertEqual(defaults["navigator_effort"], "high")
        self.assertEqual(defaults["builder_effort"], "medium")
        self.assertIn("do not default Builder to max", defaults["note"])

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "last-pairing.json"
            saved = PREFS.save_last_pairing(
                {
                    "navigator_model": "gpt-5.2",
                    "navigator_effort": "high",
                    "builder_model": "deepseek-flash",
                    "builder_effort": "medium",
                },
                path,
            )
            self.assertEqual(saved, path)
            loaded = PREFS.load_last_pairing(path)
            assert loaded is not None
            self.assertEqual(loaded["navigator_model"], "gpt-5.2")
            self.assertEqual(loaded["navigator_effort"], "high")
            self.assertEqual(loaded["builder_model"], "deepseek-flash")
            self.assertEqual(loaded["builder_effort"], "medium")
            self.assertIn("reuse last: gpt-5.2/high → deepseek-flash/medium", PREFS.format_reuse_line(loaded))

    def test_load_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "missing.json"
            self.assertIsNone(PREFS.load_last_pairing(path))

    def test_reject_builder_invalid_effort(self):
        with self.assertRaises(PREFS.PrefsError):
            PREFS.validate_pairing({"builder_effort": "ultra"})

    def test_cli_save_show_prompt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "prefs.json"
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = PREFS.main(
                    [
                        "--path",
                        str(path),
                        "save",
                        "--navigator-model",
                        "parent",
                        "--navigator-effort",
                        "high",
                        "--builder-model",
                        "deepseek-flash",
                        "--builder-effort",
                        "medium",
                        "--json",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertTrue(path.is_file())
            self.assertTrue(json.loads(buf.getvalue())["ok"])
            # show
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = PREFS.main(["--path", str(path), "show", "--json"])
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["source"], "last-pairing")
            self.assertIn("reuse last:", payload["reuse"])

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = PREFS.main(["--path", str(path), "prompt"])
            self.assertEqual(rc, 0)
            prompt = buf.getvalue()
            self.assertIn("reuse last:", prompt)
            self.assertIn("Builder effort medium", prompt)
            self.assertIn("do not default Builder to max", prompt)

    def test_env_prefs_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "env-pairing.json"
            with unittest.mock.patch.dict(os.environ, {PREFS.PREFS_ENV: str(path)}, clear=False):
                self.assertEqual(PREFS.default_prefs_path(), path)

    def test_skill_preflight_wording(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("Recommended defaults (Prewalk)", text)
        self.assertIn("Navigator effort `high`", text)
        self.assertIn("Builder effort `medium`", text)
        self.assertIn("do not default Builder to `max`", text)
        self.assertIn("prewalk_prefs.py", text)
        self.assertIn("reuse last:", text)
        self.assertIn("use defaults", text)



if __name__ == "__main__":
    unittest.main()
