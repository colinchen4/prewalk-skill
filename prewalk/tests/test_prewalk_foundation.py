from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import prewalk_builder
import prewalk_explore
import prewalk_plan


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class ExploreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self._ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.repo.parent)
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "a.py").write_text(
            "class Alpha:\n    def run(self):\n        return 'not-a-code-snippet'\n", encoding="utf-8")
        (self.repo / "src" / "b.py").write_text("from src.a import Alpha\n", encoding="utf-8")
        (self.repo / "tests" / "test_a.py").write_text("def test_alpha():\n    pass\n", encoding="utf-8")
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")

    def tearDown(self):
        if self._ceiling is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = self._ceiling
        self.tmp.cleanup()

    def test_index_is_scoped_and_contains_no_code_snippets(self):
        document = prewalk_explore.build_index(self.repo, ["src/"], ["Alpha"])
        self.assertTrue(any(e["symbol"] == "Alpha" for e in document["entries"]))
        self.assertNotIn("not-a-code-snippet", json.dumps(document))
        self.assertEqual(document["nav_digest"]["test_files"], [])
        paths = prewalk_explore.write_index(self.repo / "run", document)
        self.assertTrue(paths["index"].exists())
        self.assertTrue(paths["digest"].exists())

    def test_nav_digest_includes_tests(self):
        document = prewalk_explore.build_index(self.repo, ["."], ["Alpha"])
        self.assertEqual(document["nav_digest"]["test_files"], ["tests/test_a.py"])

    def test_digest_flags_missing_tests_and_truncation(self):
        document = prewalk_explore.build_index(self.repo, ["src/"], ["Alpha"], max_entries=1)
        self.assertIn("no_test_coverage", document["nav_digest"]["risk_flags"])
        self.assertIn("index_truncated", document["nav_digest"]["risk_flags"])

    def test_non_git_is_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaises(ValueError):
                prewalk_explore.build_index(Path(other), ["."])


class PlanTests(unittest.TestCase):
    def test_modes_define_builder_requirement(self):
        for mode, expected in (("brief_only", True), ("sample_guided", True), ("navigator_only", False)):
            plan = prewalk_plan.create_plan("h1", mode, "goal", ["verified"], ["src/"])
            self.assertEqual(plan["builder_required"], expected)

    def test_prompt_prefix_is_stable_across_phases(self):
        plan = prewalk_plan.create_plan("h1", "brief_only", "goal", ["verified"], ["src/"])
        run = Path("run")
        exploring = prewalk_builder.phase_prompt(plan, run, "EXPLORING")
        executing = prewalk_builder.phase_prompt(plan, run, "EXECUTE", "continue")
        self.assertEqual(exploring.split("CURRENT_PHASE:", 1)[0], executing.split("CURRENT_PHASE:", 1)[0])
        self.assertIn("SESSION_CACHE_KEY: prewalk/h1", exploring)
        self.assertIn("CURRENT_PHASE: EXPLORING", exploring)
        self.assertIn("CURRENT_PHASE: EXECUTE", executing)

    def test_write_and_load_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            path = prewalk_plan.write_plan(run, prewalk_plan.create_plan(
                "h1", "brief_only", "goal", ["tests pass"], ["src/"],
                model="kimi-code/kimi-for-coding", effort="low"))
            loaded = prewalk_plan.load_plan(path)
            self.assertEqual(loaded["status"], "planned")
            self.assertEqual((loaded["model"], loaded["effort"]), ("kimi-code/kimi-for-coding", "low"))

    def test_invalid_plan_is_rejected(self):
        with self.assertRaises(ValueError):
            prewalk_plan.create_plan("h1", "brief_only", "goal", ["verified"], [])
        with self.assertRaises(ValueError):
            prewalk_plan.create_plan("h1", "brief_only", "goal", ["verified"], ["src/"],
                                     "x" * (prewalk_plan.MAX_CONTEXT_CHARS + 1))
        with self.assertRaises(ValueError):
            prewalk_plan.validate_plan({"schema_version": 1, "status": "running"})


if __name__ == "__main__":
    unittest.main()
