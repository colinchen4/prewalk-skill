from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=ResourceWarning, message="subprocess")

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


LAYERS = _load("prewalk_layers_under_test", "prewalk_layers.py")
ASYNC = _load("prewalk_async_layers_under_test", "prewalk_async.py")
LayerError = LAYERS.LayerError

# A fake weak Builder that really edits the repo: one handler file per step,
# then reports progress.  Behaviour knobs come from a JSON argv.
FAKE_BUILDER = r'''
import json, sys, time, pathlib
progress, repo, cfg = sys.argv[1], pathlib.Path(sys.argv[2]), json.loads(pathlib.Path(sys.argv[3]).read_text())
sys.stdin.read()
print(json.dumps({"type": "session", "session_id": "sess-1"}), flush=True)
done = []
for i in range(cfg.get("steps", 2)):
    if cfg.get("crash_at") == i:
        (repo / "src/handlers/partial.py").write_text("def partial():\n")
        sys.exit(3)
    path = f"src/handlers/h{i}.py"
    (repo / path).write_text(f"def h{i}():\n    return {i}\n")
    done.append(path)
    if cfg.get("outside") and i == 0:
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs/notes.md").write_text("builder notes\n")
    if cfg.get("edit_template") and i == 0:
        app = repo / "src/app.py"
        lines = app.read_text().splitlines()
        lines[2] = "TEMPLATE = 'rewritten by builder'"
        app.write_text("\n".join(lines) + "\n")
    if cfg.get("append_after_template") and i == 0:
        app = repo / "src/app.py"
        app.write_text(app.read_text() + "TAIL = 1\n")
    with open(progress, "a") as fh:
        fh.write(json.dumps({"state": "step_completed", "summary": f"wrote {path}",
                             "changed_paths": [path]}) + "\n")
    time.sleep(cfg.get("delay", 0.4))
claimed = cfg.get("claim", done)
text = ("done\n```text\nPREWALK_PHASE: NAVIGATOR_REVIEW\nHANDOFF_ID: " + cfg.get("handoff_id", "h1") + "\n"
        "CHANGED:\n" + "".join(f"- {p}\n" for p in claimed) + "VALIDATION: fake\n```")
print(json.dumps({"type": "final", "session_id": "sess-1", "text": text, "completed": True}), flush=True)
'''

APP = "".join(f"line_{i} = {i}\n" for i in range(1, 11))


def sh(cwd: Path, *args: str) -> str:
    # GIT_OPTIONAL_LOCKS=0: the test's own `git status` must not refresh the real index
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout


class RepoBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self._ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.base)
        self.repo = self.base / "repo"
        self.run = self.base / "run"
        self.repo.mkdir()
        sh(self.repo, "init", "-q", "-b", "main")
        sh(self.repo, "config", "user.email", "u@example.com")
        sh(self.repo, "config", "user.name", "u")
        (self.repo / "src/handlers").mkdir(parents=True)
        (self.repo / "src/app.py").write_text(APP)
        (self.repo / "src/old.py").write_text("old = 1\n")
        (self.repo / "logo.bin").write_bytes(b"\x00\x01\x02")
        (self.repo / ".gitignore").write_text("*.log\nbuild/\n")
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-q", "-m", "base")
        self.head = sh(self.repo, "rev-parse", "HEAD").strip()

    def tearDown(self) -> None:
        state = ASYNC._read_json(self.run / "state.json", {})
        if state and state.get("status") not in ASYNC.TERMINAL:
            ASYNC.cancel(self.run, timeout=4)
        if self._ceiling is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = self._ceiling
        self.temp.cleanup()

    def first_edit(self, run_dir: Path | None = None) -> None:
        """Navigator's template edit: change line 3, add one handler (untracked)."""
        LAYERS.snapshot(self.repo, run_dir or self.run, "h1", "L0", "navigator", "inherited baseline")
        lines = APP.splitlines()
        lines[2] = "line_3 = handler_registry()  # template"
        (self.repo / "src/app.py").write_text("\n".join(lines) + "\n")
        (self.repo / "src/handlers/base.py").write_text("def base():\n    return 0\n")

    def real_state(self) -> tuple[bytes, str, str, str]:
        index = (self.repo / ".git/index").read_bytes()
        return (index, sh(self.repo, "rev-parse", "HEAD"), sh(self.repo, "symbolic-ref", "HEAD"),
                sh(self.repo, "status", "--porcelain=v1", "--untracked-files=all"))


class SnapshotTests(RepoBase):
    def test_snapshot_captures_untracked_respects_gitignore_and_touches_nothing(self):
        self.first_edit()
        (self.repo / "debug.log").write_text("ignored\n")
        (self.repo / "build").mkdir()
        (self.repo / "build/out.o").write_text("ignored\n")
        sh(self.repo, "add", "src/app.py")  # a staged change must survive too
        before = self.real_state()
        mtime = os.stat(self.repo / ".git/index").st_mtime_ns
        rec = LAYERS.snapshot(self.repo, self.run, "h1", "L1", "navigator", "first edit")
        after = self.real_state()
        self.assertEqual(before[0], after[0], "real index must be byte-identical")
        self.assertEqual(mtime, os.stat(self.repo / ".git/index").st_mtime_ns)
        self.assertEqual(before[1:], after[1:])
        files = sh(self.repo, "ls-tree", "-r", "--name-only", rec["sha"]).split()
        self.assertIn("src/handlers/base.py", files)
        self.assertNotIn("debug.log", files)
        self.assertNotIn("build/out.o", files)
        l0 = sh(self.repo, "rev-parse", "refs/prewalk/h1/L0").strip()
        self.assertEqual(sh(self.repo, "rev-parse", "refs/prewalk/h1/L1").strip(), rec["sha"])
        self.assertEqual(rec["parent"], l0)
        self.assertFalse(list(self.run.glob(".prewalk-index-*")), "temp index removed")
        self.assertTrue((self.run / "first-edit.patch").read_text().startswith("diff --git"))
        doc = json.loads((self.run / "layers.json").read_text())
        self.assertEqual(doc["base"]["parent"], self.head)
        self.assertEqual(doc["base"]["manifest"], [])
        self.assertEqual([l["name"] for l in doc["layers"]], ["L1"])
        for key in ("name", "sha", "parent", "via", "message", "ts", "manifest", "totals"):
            self.assertIn(key, doc["layers"][0])

    def test_manifest_counts_add_modify_delete_and_binary(self):
        self.first_edit()
        (self.repo / "src/old.py").unlink()
        (self.repo / "logo.bin").write_bytes(b"\x00\x09\x09\x09")
        rec = LAYERS.snapshot(self.repo, self.run, "h1", "L1")
        got = {e["path"]: (e["status"], e["added"], e["deleted"]) for e in rec["manifest"]}
        self.assertEqual(got, {"src/app.py": ("M", 1, 1), "src/handlers/base.py": ("A", 2, 0),
                               "src/old.py": ("D", 0, 1), "logo.bin": ("M", None, None)})
        self.assertEqual(rec["totals"], {"files": 4, "added": 3, "deleted": 2})

    def test_run_dir_inside_repo_is_never_snapshotted(self):
        run = self.repo / ".prewalk-run"
        self.first_edit(run)
        rec = LAYERS.snapshot(self.repo, run, "h1", "L1")
        rec2 = LAYERS.snapshot(self.repo, run, "h1")
        files = sh(self.repo, "ls-tree", "-r", "--name-only", rec2["sha"]).split()
        self.assertFalse([f for f in files if f.startswith(".prewalk-run")])
        self.assertEqual(rec2["manifest"], [])
        self.assertEqual(rec["name"], "L1")

    def test_missing_index_falls_back_to_head_and_unborn_repo_works(self):
        (self.repo / ".git/index").unlink()
        LAYERS.snapshot(self.repo, self.run, "h1", "L0")
        rec = LAYERS.snapshot(self.repo, self.run, "h1", "L1")
        self.assertEqual(rec["manifest"], [])  # worktree == HEAD
        self.assertFalse((self.repo / ".git/index").exists())
        fresh = self.base / "fresh"
        fresh.mkdir()
        sh(fresh, "init", "-q")
        LAYERS.snapshot(fresh, self.base / "run2", "h2", "L0")
        (fresh / "a.txt").write_text("a\n")
        rec = LAYERS.snapshot(fresh, self.base / "run2", "h2", "L1")
        self.assertEqual([e["path"] for e in rec["manifest"]], ["a.txt"])
        self.assertNotEqual(sh(fresh, "symbolic-ref", "HEAD").strip(), "")
        self.assertEqual(subprocess.run(["git", "-C", str(fresh), "rev-parse", "-q", "--verify", "HEAD"],
                                        capture_output=True).returncode, 1)

    def test_rejects_duplicate_layer_bad_ids_and_non_git(self):
        LAYERS.snapshot(self.repo, self.run, "h1", "L0")
        LAYERS.snapshot(self.repo, self.run, "h1", "L1")
        with self.assertRaises(LayerError):
            LAYERS.snapshot(self.repo, self.run, "h1", "L1")
        with self.assertRaises(LayerError):
            LAYERS.snapshot(self.repo, self.run, "h2", "L2-9")
        with self.assertRaises(LayerError):
            LAYERS.snapshot(self.repo, self.base / "r3", "../x", "L1")
        with self.assertRaises(LayerError):
            LAYERS.snapshot(self.base, self.base / "r4", "h1", "L1")

    def test_l0_separates_inherited_dirty_changes(self):
        (self.repo / "inherited.txt").write_text("keep me\n")
        self.first_edit()
        rec = LAYERS.snapshot(self.repo, self.run, "h1", "L1")
        self.assertNotIn("inherited.txt", {e["path"] for e in rec["manifest"]})
        doc = LAYERS.load(self.run)
        self.assertIn("inherited.txt", {e["path"] for e in doc["base"]["manifest"]})
        self.assertNotIn("inherited.txt", (self.run / "first-edit.patch").read_text())

    def test_first_edit_section_is_capped_with_truncation_note(self):
        LAYERS.snapshot(self.repo, self.run, "h1", "L0")
        (self.repo / "src/big.py").write_text("".join(f"x{i} = {i}\n" for i in range(2000)))
        LAYERS.snapshot(self.repo, self.run, "h1", "L1")
        section = LAYERS.first_edit_section(self.run, max_chars=800)
        self.assertIn("A src/big.py +2000 -0", section)
        self.assertIn(LAYERS.TEMPLATE_INSTRUCTION, section)
        self.assertIn("[truncated: showing", section)
        self.assertIn("first-edit.patch", section)
        self.assertLess(len(section), 800 + 1200)
        self.assertIn("x1999 = 1999", (self.run / "first-edit.patch").read_text())

    def test_cleanup_deletes_only_this_handoff_refs(self):
        LAYERS.snapshot(self.repo, self.run, "h1", "L0")
        LAYERS.snapshot(self.repo, self.run, "h1", "L1")
        LAYERS.snapshot(self.repo, self.run, "h1")
        LAYERS.snapshot(self.repo, self.base / "other", "h9", "L0")
        LAYERS.snapshot(self.repo, self.base / "other", "h9", "L1")
        out = LAYERS.cleanup(self.run, None, "h1")
        self.assertEqual(sorted(out["deleted"]), ["refs/prewalk/h1/L0", "refs/prewalk/h1/L1",
                                                  "refs/prewalk/h1/L2-1"])
        refs = sh(self.repo, "for-each-ref", "--format=%(refname)", "refs/prewalk/").split()
        self.assertEqual(sorted(refs), ["refs/prewalk/h9/L0", "refs/prewalk/h9/L1"])
        self.assertTrue((self.run / "layers.json").exists())
        self.assertEqual(sh(self.repo, "rev-parse", "HEAD").strip(), self.head)


class TemplateOverlapTests(RepoBase):
    def _review_after(self, mutate) -> dict:
        self.first_edit()
        LAYERS.snapshot(self.repo, self.run, "h1", "L1")
        mutate()
        LAYERS.snapshot(self.repo, self.run, "h1", None, "builder", "step")
        return LAYERS.review_report(self.run, scope=["src/"])

    def _flags(self, report) -> set[str]:
        return {f["flag"] for f in report["flags"]}

    def test_editing_template_line_is_flagged(self):
        def mutate():
            text = (self.repo / "src/app.py").read_text().replace("# template", "# changed")
            (self.repo / "src/app.py").write_text(text)
        self.assertIn("template_edited", self._flags(self._review_after(mutate)))

    def test_deleting_template_file_is_flagged(self):
        self.assertIn("template_edited", self._flags(
            self._review_after(lambda: (self.repo / "src/handlers/base.py").unlink())))

    def test_edits_elsewhere_in_template_file_are_not_flagged(self):
        def mutate():
            app = self.repo / "src/app.py"
            app.write_text(app.read_text().replace("line_9 = 9", "line_9 = 99") + "tail = 1\n")
        report = self._review_after(mutate)
        self.assertNotIn("template_edited", self._flags(report))
        self.assertEqual(report["builder"][0]["path"], "src/app.py")


class AsyncLayerTests(RepoBase):
    def setUp(self) -> None:
        super().setUp()
        self.fake = self.base / "fake_builder.py"
        self.fake.write_text(FAKE_BUILDER, encoding="utf-8")

    def brief(self, **cfg) -> Path:
        cfg_path = self.base / "builder-cfg.json"  # argv is str.format()ed: no braces
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        data = {"handoff_id": "h1", "host": "generic", "goal": "Add the other handlers like L1",
                "success": ["handlers exist"], "scope": ["src/handlers/", "src/app.py"],
                "cwd": str(self.repo),
                "argv": [sys.executable, str(self.fake), "{progress}", str(self.repo), str(cfg_path)]}
        path = self.base / "brief.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def layers(self) -> list[dict]:
        return json.loads((self.run / "layers.json").read_text())["layers"]

    def flags(self) -> dict[str, dict]:
        return {f["flag"]: f for f in json.loads((self.run / "review.json").read_text())["flags"]}

    def test_launch_injects_first_edit_and_supervisor_layers_each_step(self):
        self.first_edit()
        before = self.real_state()
        ASYNC.launch(self.run, self.brief(steps=3, delay=0.5))
        brief = (self.run / "brief.txt").read_text()
        self.assertIn("FIRST EDIT = layer L1", brief)
        self.assertIn("M src/app.py +1 -1", brief)
        self.assertIn("A src/handlers/base.py +2 -0", brief)
        self.assertIn(LAYERS.TEMPLATE_INSTRUCTION, brief)
        self.assertIn("+line_3 = handler_registry()  # template", brief)
        done = ASYNC.wait(self.run, timeout=20)
        self.assertEqual(done["status"], "completed")
        self.assertTrue(done["review_report"].endswith("review.md"))
        names = [l["name"] for l in self.layers()]
        self.assertEqual(names, ["L1", "L2-1", "L2-2", "L2-3"])
        for i, layer in enumerate(self.layers()[1:]):
            self.assertEqual(layer["via"], "builder")
            self.assertEqual([e["path"] for e in layer["manifest"]], [f"src/handlers/h{i}.py"])
            self.assertIsInstance(layer["progress_seq"], int)
        self.assertEqual(self.flags(), {})
        md = (self.run / "review.md").read_text()
        self.assertIn("## Flags\n- none", md)
        self.assertIn("Builder (L1..L2-3): 3 files +6 -0", md)
        self.assertIn("+def h2():", md)
        after = self.real_state()
        self.assertEqual(before[0], after[0], "real index byte-identical after the whole run")
        self.assertEqual(before[1:3], after[1:3])
        last = ASYNC.status(self.run)["new_events"][-1]
        self.assertIn("layer review:", last["summary"])

    def test_crash_leaves_layers_and_final_snapshot(self):
        self.first_edit()
        ASYNC.launch(self.run, self.brief(steps=4, delay=0.4, crash_at=2))
        done = ASYNC.wait(self.run, timeout=20)
        self.assertEqual(done["status"], "failed")
        layers = self.layers()
        captured = {e["path"] for layer in layers if layer["name"].startswith("L2-")
                    for e in layer["manifest"]}
        self.assertEqual(captured, {"src/handlers/h0.py", "src/handlers/h1.py", "src/handlers/partial.py"})
        self.assertIn("no_return_package", self.flags())
        last_ref = layers[-1]["ref"]
        self.assertIn(last_ref, sh(self.repo, "for-each-ref", "refs/prewalk/"))

    def test_cancel_leaves_layers_and_review(self):
        self.first_edit()
        ASYNC.launch(self.run, self.brief(steps=50, delay=0.3))
        self.wait_layers(2)
        out = ASYNC.cancel(self.run, timeout=6)
        self.assertEqual(out["status"], "cancelled")
        names = [l["name"] for l in self.layers()]
        self.assertGreaterEqual(len(names), 3)
        self.assertTrue((self.run / "review.md").exists())
        self.assertIn("Builder status: cancelled", (self.run / "review.md").read_text())

    @unittest.skipIf(os.name == "nt", "POSIX process-group kill test")
    def test_layers_survive_a_killed_supervisor(self):
        self.first_edit()
        ASYNC.launch(self.run, self.brief(steps=50, delay=0.3))
        self.wait_layers(2)
        state = json.loads((self.run / "state.json").read_text())
        os.kill(state["supervisor_pid"], signal.SIGKILL)
        os.killpg(state["builder_pid"], signal.SIGKILL)
        time.sleep(0.2)
        report = LAYERS.review_report(self.run)  # Navigator can still review by hand
        self.assertGreaterEqual(len(report["layers"]), 3)
        self.assertIn("no_return_package", {f["flag"] for f in report["flags"]})
        (self.run / "state.json").write_text(json.dumps({**state, "status": "failed"}))

    def wait_layers(self, n: int) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            doc = LAYERS.load(self.run) or {"layers": []}
            if sum(l["name"].startswith("L2-") for l in doc["layers"]) >= n:
                return
            time.sleep(0.05)
        self.fail("builder layers not created")

    def test_flags_empty_out_of_scope_template_and_claim_mismatch(self):
        self.first_edit()
        ASYNC.launch(self.run, self.brief(steps=0))
        ASYNC.wait(self.run, timeout=20)
        self.assertIn("empty_after_L1", self.flags())
        self.assertIn("no_builder_layers", self.flags())

    def test_flags_out_of_scope_template_edit_and_claim_mismatch(self):
        self.first_edit()
        ASYNC.launch(self.run, self.brief(steps=2, delay=0.3, outside=True, edit_template=True,
                                          claim=["src/handlers/h0.py", "src/handlers/h7.py"]))
        self.assertEqual(ASYNC.wait(self.run, timeout=20)["status"], "completed")
        flags = self.flags()
        self.assertEqual(flags["out_of_scope"]["paths"], ["docs/notes.md"])
        self.assertEqual(flags["template_edited"]["hunks"][0]["path"], "src/app.py")
        self.assertEqual(flags["claim_mismatch"]["claimed_not_changed"], ["src/handlers/h7.py"])
        self.assertEqual(flags["claim_mismatch"]["changed_not_claimed"],
                         ["docs/notes.md", "src/app.py", "src/handlers/h1.py"])
        md = (self.run / "review.md").read_text()
        self.assertIn("**out_of_scope**", md)
        self.assertIn("**template_edited**", md)

    def test_appending_after_template_is_not_flagged(self):
        self.first_edit()
        ASYNC.launch(self.run, self.brief(steps=1, delay=0.3, append_after_template=True,
                                          claim=["src/handlers/h0.py", "src/app.py"]))
        ASYNC.wait(self.run, timeout=20)
        self.assertEqual(self.flags(), {})

    def test_non_git_cwd_and_opt_out_disable_layers(self):
        brief = json.loads(self.brief(steps=1, delay=0.1).read_text())
        brief["layers"] = False
        path = self.base / "b2.json"
        path.write_text(json.dumps(brief))
        ASYNC.launch(self.run, path)
        self.assertEqual(ASYNC.wait(self.run, timeout=20)["status"], "completed")
        self.assertFalse((self.run / "layers.json").exists())
        self.assertNotIn("FIRST EDIT", (self.run / "brief.txt").read_text())

    def test_cli_review_and_cleanup(self):
        self.first_edit()
        ASYNC.launch(self.run, self.brief(steps=1, delay=0.2))
        ASYNC.wait(self.run, timeout=20)
        out = subprocess.run([sys.executable, str(SCRIPTS / "prewalk_layers.py"), "layer-diff",
                              "--run-dir", str(self.run)], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["layers"], ["L1", "L2-1"])
        out = subprocess.run([sys.executable, str(SCRIPTS / "prewalk_async.py"), "review",
                              "--run-dir", str(self.run)], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        out = subprocess.run([sys.executable, str(SCRIPTS / "prewalk_layers.py"), "cleanup",
                              "--handoff-id", "h1", "--run-dir", str(self.run)],
                             capture_output=True, text=True)
        self.assertEqual(len(json.loads(out.stdout)["deleted"]), 3)
        self.assertEqual(sh(self.repo, "for-each-ref", "refs/prewalk/"), "")


class SeededIndexTimingTests(unittest.TestCase):
    FILES = 4000

    def test_seeded_index_is_faster_than_empty_index_and_same_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "big"
            repo.mkdir()
            sh(repo, "init", "-q")
            payload = "x" * 8192
            for i in range(self.FILES):
                d = repo / f"pkg{i % 40}"
                d.mkdir(exist_ok=True)
                (d / f"m{i}.py").write_text(f"# {i}\n{payload}\n")
            sh(repo, "add", "-A")
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=u", "-c", "user.email=u@x",
                            "commit", "-q", "-m", "base"], check=True)
            time.sleep(1.1)  # leave the racy-git window, then let git settle the index
            subprocess.run(["git", "-C", str(repo), "update-index", "-q", "--refresh"], check=True)
            for i in range(5):
                (repo / f"pkg{i}" / f"m{i}.py").write_text("changed\n")
            (repo / "new.py").write_text("new\n")
            scratch = Path(tmp) / "scratch"
            LAYERS.capture_tree(repo, scratch)  # warm the page cache / git startup
            timings, trees = {}, {}
            for mode in ("index", "read-tree", "empty"):
                best = None
                for _ in range(3):
                    t0 = time.perf_counter()
                    trees[mode] = LAYERS.capture_tree(repo, scratch, seed=mode)
                    dt = time.perf_counter() - t0
                    best = dt if best is None else min(best, dt)
                timings[mode] = best
            self.assertEqual(len(set(trees.values())), 1)
            self.assertLess(timings["index"], timings["empty"])
            line = ", ".join(f"{k}={v * 1000:.0f}ms" for k, v in timings.items())
            out = os.environ.get("PREWALK_TIMING_OUT")
            if out:
                Path(out).write_text(f"{self.FILES} files: {line}\n")
            print(f"\n[timing] {self.FILES} files, 6 changed: {line}", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
