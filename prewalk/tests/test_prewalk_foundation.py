from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
import fakebins
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
            "class Alpha:\n    def run(self):\n        return 'not-a-code-snippet'\n"
            "    def runner(self):\n        return Alpha().run()\n", encoding="utf-8")
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

    def test_references_use_word_boundaries_and_include_untracked(self):
        (self.repo / "src" / "c.py").write_text("Alpha\nAlphabetical\n", encoding="utf-8")
        document = prewalk_explore.build_index(self.repo, ["src/"], ["Alpha"])
        self.assertEqual(document["nav_digest"]["engine"]["references"], "git-grep")
        c_refs = [e for e in document["entries"]
                  if e["path"] == "src/c.py" and e["relation"] == "reference"]
        self.assertEqual([e["line"] for e in c_refs], [1])

    def test_definitions_are_ast_qualified_with_spans(self):
        document = prewalk_explore.build_index(self.repo, ["src/"], ["Alpha"])
        defs = {e["symbol"]: e for e in document["entries"] if e["relation"] == "definition"}
        self.assertIn("Alpha.run", defs)
        self.assertEqual(defs["Alpha"]["line"], 1)
        self.assertEqual(defs["Alpha"]["end_line"], 5)
        self.assertEqual(defs["Alpha.run"]["line"], 2)
        self.assertEqual(defs["Alpha.run"]["end_line"], 3)
        definition_lines = {(e["path"], e["line"]) for e in defs.values()}
        self.assertFalse(any((e["path"], e["line"]) in definition_lines
                             for e in document["entries"] if e["relation"] == "reference"))

    def test_python_syntax_error_falls_back_to_regex(self):
        (self.repo / "src" / "bad.py").write_text("def broken(:\n    pass\ndef fine():\n    pass\n",
                                                 encoding="utf-8")
        document = prewalk_explore.build_index(self.repo, ["src/"], ["fine"])
        syms = [e["symbol"] for e in document["entries"] if e["relation"] == "definition"]
        self.assertIn("fine", syms)

    def test_engine_reports_ctags_unavailable_as_regex(self):
        with mock.patch("prewalk_explore.shutil.which", return_value=None):
            document = prewalk_explore.build_index(self.repo, ["src/"], ["Alpha"])
        self.assertEqual(document["nav_digest"]["engine"]["definitions"], "ast+regex")

    def test_fake_universal_ctags_reports_ast_ctags(self):
        (self.repo / "src" / "mod.js").write_text("function beta() {\n    return 1\n}\n",
                                                 encoding="utf-8")
        bin_dir = fakebins.make_bin_dir(Path(self.tmp.name) / "fake", names=("ctags",))
        real = prewalk_explore.shutil.which

        def which(name):
            if name == "ctags":
                return str(bin_dir / ("ctags.cmd" if os.name == "nt" else "ctags"))
            return real(name)

        with mock.patch("prewalk_explore.shutil.which", side_effect=which):
            document = prewalk_explore.build_index(self.repo, ["src/"], ["beta"])
        self.assertEqual(document["nav_digest"]["engine"]["definitions"], "ast+ctags")
        syms = [e["symbol"] for e in document["entries"] if e["relation"] == "definition"]
        self.assertIn("beta", syms)

    def test_candidates_ranked_entry_term_first(self):
        document = prewalk_explore.build_index(self.repo, ["."], ["Alpha"])
        top = document["nav_digest"]["candidate_l1"]
        self.assertIsNotNone(top)
        self.assertEqual(top["symbol"], "Alpha")
        self.assertGreaterEqual(top["score"], 100)
        candidates = document["nav_digest"]["candidates"]
        self.assertLessEqual(len(candidates), 3)
        self.assertEqual(candidates[0]["symbol"], "Alpha")

    def test_query_filters_limit_and_strips_notes(self):
        document = prewalk_explore.build_index(self.repo, ["."], ["Alpha"])
        prewalk_explore.write_index(self.repo / "run", document)
        result = prewalk_explore.query_index(self.repo / "run", symbol="Alpha")
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"][0]["symbol"], "Alpha")
        self.assertFalse(any("note" in e for e in result["entries"]))
        limited = prewalk_explore.query_index(self.repo / "run", limit=1)
        self.assertEqual(limited["returned"], 1)
        self.assertGreater(limited["total"], 1)
        by_path = prewalk_explore.query_index(self.repo / "run", path="src/b.py")
        self.assertTrue(all("src/b.py" in e["path"] for e in by_path["entries"]))
        by_rel = prewalk_explore.query_index(self.repo / "run", relation="reference")
        self.assertTrue(all(e["relation"] == "reference" for e in by_rel["entries"]))


    def test_query_path_is_prefix_not_substring(self):
        document = prewalk_explore.build_index(self.repo, ["."], ["Alpha"])
        prewalk_explore.write_index(self.repo / "run", document)
        prefix = prewalk_explore.query_index(self.repo / "run", path="src")
        self.assertTrue(prefix["entries"])
        self.assertTrue(all(e["path"].startswith("src") for e in prefix["entries"]))
        substring = prewalk_explore.query_index(self.repo / "run", path="c/b.py")
        self.assertEqual(substring["entries"], [])

    def test_truncation_keeps_references_and_ranks_full_list(self):
        for i in range(6):
            (self.repo / "src" / f"m{i}.py").write_text(
                f"def filler{i}():\n    pass\n", encoding="utf-8")
        document = prewalk_explore.build_index(self.repo, ["src/"], ["Alpha"], max_entries=3)
        self.assertTrue(document["truncated"])
        refs = [e for e in document["entries"] if e["relation"] == "reference"]
        self.assertTrue(refs)
        self.assertLessEqual(len(document["entries"]), 3)
        top = document["nav_digest"]["candidate_l1"]
        self.assertIsNotNone(top)
        self.assertEqual(top["symbol"], "Alpha")

    def test_ctags_batch_indexes_multiple_files(self):
        (self.repo / "src" / "one.js").write_text("function uno() { return 1 }\n",
                                                  encoding="utf-8")
        (self.repo / "src" / "two.js").write_text("class Dos { }\n", encoding="utf-8")
        bin_dir = fakebins.make_bin_dir(Path(self.tmp.name) / "fake2", names=("ctags",))
        real = prewalk_explore.shutil.which

        def which(name):
            if name == "ctags":
                return str(bin_dir / ("ctags.cmd" if os.name == "nt" else "ctags"))
            return real(name)

        with mock.patch("prewalk_explore.shutil.which", side_effect=which):
            document = prewalk_explore.build_index(self.repo, ["src/"], ["uno"])
        self.assertEqual(document["nav_digest"]["engine"]["definitions"], "ast+ctags")
        syms = {e["symbol"] for e in document["entries"] if e["relation"] == "definition"}
        self.assertIn("uno", syms)
        self.assertIn("Dos", syms)


class PlanTests(unittest.TestCase):
    def test_modes_define_builder_requirement(self):
        for mode, expected in (("brief_only", True), ("sample_guided", True), ("navigator_only", False)):
            plan = prewalk_plan.create_plan("h1", mode, "goal", ["verified"], ["src/"])
            self.assertEqual(plan["builder_required"], expected)

    def test_explore_defaults_false_and_old_plans_stay_valid(self):
        plan = prewalk_plan.create_plan("h1", "brief_only", "goal", ["verified"], ["src/"])
        self.assertFalse(plan["explore"])
        self.assertTrue(prewalk_plan.create_plan("h1", "brief_only", "goal", ["verified"],
                                                 ["src/"], explore=True)["explore"])

    def test_prompt_prefix_is_stable_across_phases(self):
        plan = prewalk_plan.create_plan("h1", "brief_only", "goal", ["verified"], ["src/"])
        run = Path("run")
        exploring = prewalk_builder.phase_prompt(plan, run, "EXPLORING")
        executing = prewalk_builder.phase_prompt(plan, run, "EXECUTE", "continue")
        self.assertEqual(exploring.split("CURRENT_PHASE:", 1)[0], executing.split("CURRENT_PHASE:", 1)[0])
        self.assertNotIn("SESSION_CACHE_KEY", exploring)
        self.assertTrue(exploring.startswith("PREWALK_PHASE: PERSISTENT_BUILDER\nHANDOFF_ID: h1"))
        self.assertIn("CURRENT_PHASE: EXPLORING", exploring)
        self.assertIn("CURRENT_PHASE: EXECUTE", executing)
        self.assertIn("NOTE:", executing)

    def test_follow_up_prompt_carries_only_the_delta(self):
        plan = prewalk_plan.create_plan("h1", "brief_only", "goal", ["verified"], ["src/"])
        follow = prewalk_builder.phase_prompt(plan, Path("run"), "EXECUTE", "fix the second assert",
                                              include_plan=False)
        self.assertNotIn("GOAL:", follow)
        self.assertIn("HANDOFF_ID: h1", follow)
        self.assertIn("CURRENT_PHASE: EXECUTE", follow)
        self.assertIn("NEW DELTA:", follow)
        self.assertIn("NOTE:", follow)

    def test_execution_index_prompt_mentions_query(self):
        plan = prewalk_plan.create_plan("h1", "brief_only", "goal", ["verified"], ["src/"])
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "exploration-index.json").write_text("{}", encoding="utf-8")
            text = prewalk_builder.phase_prompt(plan, run, "EXECUTE")
            self.assertIn("query", text)
            self.assertIn("do not open exploration-index.json", text)

    def test_write_and_load_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            path = prewalk_plan.write_plan(run, prewalk_plan.create_plan(
                "h1", "brief_only", "goal", ["tests pass"], ["src/"],
                model="kimi-code/kimi-for-coding", effort="low"))
            loaded = prewalk_plan.load_plan(path)
            self.assertEqual(loaded["status"], "planned")
            self.assertIn("created_at", loaded)
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
