from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import prewalk_explore
import prewalk_hash


class HashTests(unittest.TestCase):
    def test_json_write_skips_identical_content_and_volatile_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.json"
            first = {"generated_at": "one", "entries": [{"path": "a.py"}]}
            changed, fingerprint = prewalk_hash.write_json_if_changed(path, first, {"generated_at"})
            self.assertFalse(changed)
            before = path.stat().st_mtime_ns
            second = {"generated_at": "two", "entries": [{"path": "a.py"}]}
            changed, second_fingerprint = prewalk_hash.write_json_if_changed(path, second, {"generated_at"})
            self.assertTrue(changed)
            self.assertEqual(fingerprint, second_fingerprint)
            self.assertEqual(path.stat().st_mtime_ns, before)
            self.assertEqual(json.loads(path.read_text())["generated_at"], "one")

    def test_text_and_file_hashes_skip_only_unchanged_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.patch"
            self.assertFalse(prewalk_hash.write_text_if_changed(path, "diff\n"))
            self.assertTrue(prewalk_hash.write_text_if_changed(path, "diff\n"))
            self.assertFalse(prewalk_hash.write_text_if_changed(path, "diff --git\n"))
            self.assertEqual(prewalk_hash.sha256_file(path), prewalk_hash.sha256_text("diff --git\n"))

    def test_index_fingerprint_ignores_generation_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            document = {"generated_at": "one", "scope": ["src/"], "entry_terms": [],
                        "entries": [], "truncated": False,
                        "nav_digest": {"schema_version": 1, "files": 0}}
            first = prewalk_explore.write_index(run, document)
            document["generated_at"] = "two"
            second = prewalk_explore.write_index(run, document)
            self.assertFalse(first["unchanged"])
            self.assertTrue(second["unchanged"])
            self.assertEqual(first["fingerprint"], second["fingerprint"])
            self.assertEqual(json.loads((run / "exploration-index.json").read_text())["generated_at"], "one")


if __name__ == "__main__":
    unittest.main()
