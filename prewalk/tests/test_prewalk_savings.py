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
import prewalk_plan
import prewalk_savings

NAV_PRICE = {"input": 10, "cache_read": 1, "cache_write": 2, "output": 30}
BUILDER_PRICE = {"input": 1, "cache_read": 0.1, "cache_write": 0.2, "output": 3}
NAV_TOKENS = {"input": 1000, "cache_read": 500, "cache_write": 0, "output": 200}

# Hand-computed expectations (USD, $/1M-token prices):
# nav_cost    = (1000*10 + 500*1 + 0*2 + 200*30) / 1e6 = 0.0165
# builder_cost= (5000*1 + 0*0.1 + 1000*0.2 + 500*3) / 1e6 = 0.0067
# actual      = 0.0165 + 0.0067 = 0.0232
# baseline    = 0.0165 + (5000*10 + 0*1 + 1000*2 + 500*30)/1e6 = 0.0165 + 0.067 = 0.0835
# saved       = 0.0835 - 0.0232 = 0.0603
# savings_pct = 1 - 0.0232/0.0835 = 0.7221567... -> 72.2
EXPECTED = {"actual": 0.0232, "baseline": 0.0835, "saved": 0.0603, "savings_pct": 72.2}
# builder_only: builder at nav price = 0.067 -> 1 - 0.0067/0.067 = 0.9 -> 90.0
EXPECTED_ONLY = {"builder_cost": 0.0067, "builder_at_nav": 0.067, "pct": 90.0}


def wire_record(time_ms: int, usage: dict, model: str = "nav/m",
                extra: str = "") -> dict:
    record = {"type": "usage.record", "time": time_ms, "model": model, "usage": usage}
    if extra:
        record["text"] = extra
    return record


class SavingsFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run = (self.root / "run").resolve()
        self.run.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.run / "builder-usage.json").write_text(json.dumps({
            "totals": {"inputOther": 5000, "inputCacheRead": 0,
                       "inputCacheCreation": 1000, "output": 500},
            "models": ["b/m"]}), encoding="utf-8")
        plan = prewalk_plan.validate_plan({
            "schema_version": 1, "status": "planned", "handoff_id": "h1",
            "mode": "brief_only", "goal": "g", "success": ["s"], "scope": ["x"],
            "cwd": str(self.repo), "validation": [],
            "created_at": "2026-01-01T00:00:00+00:00"})
        (self.run / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        self.state = {"handoff_id": "h1", "backend": "mock", "session_id": "builder-sess",
                      "created_at": "2026-01-01T00:00:00+00:00",
                      "ended_at": "2026-01-01T01:00:00+00:00",
                      "plan": str(self.run / "plan.json")}
        (self.run / "state.json").write_text(json.dumps(self.state), encoding="utf-8")
        self.prices = self.root / "prices.json"
        self.prices.write_text(json.dumps({"nav/m": NAV_PRICE, "b/m": BUILDER_PRICE}),
                               encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {
            "PREWALK_PRICES_PATH": str(self.prices),
            "PREWALK_HOME": str(self.root / "home")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def report(self) -> dict:
        return prewalk_savings.build_report(self.run)


class FormulaTests(SavingsFixture):
    def setUp(self):
        super().setUp()
        (self.run / "usage.json").write_text(json.dumps({
            "navigator": {"usage_status": "recorded",
                          "totals": {"inputOther": 1000, "inputCacheRead": 500,
                                     "inputCacheCreation": 0, "output": 200},
                          "models": ["nav/m"]}}), encoding="utf-8")

    def test_full_mode_exact_numbers(self):
        report = self.report()
        self.assertEqual(report["mode"], "full")
        self.assertAlmostEqual(report["actual"], EXPECTED["actual"])
        self.assertAlmostEqual(report["baseline"], EXPECTED["baseline"])
        self.assertAlmostEqual(report["saved"], EXPECTED["saved"])
        self.assertEqual(report["savings_pct"], EXPECTED["savings_pct"])
        self.assertAlmostEqual(report["savings_fraction"], 1 - 0.0232 / 0.0835)
        self.assertEqual(report["models"], {"navigator": "nav/m", "builder": "b/m"})

    def test_cache_categories_priced_separately(self):
        tokens = {"input": 0, "cache_read": 1000000, "cache_write": 0, "output": 0}
        self.assertEqual(prewalk_savings.cost(tokens, NAV_PRICE), 1.0)
        tokens = {"input": 1000000, "cache_read": 0, "cache_write": 0, "output": 0}
        self.assertEqual(prewalk_savings.cost(tokens, NAV_PRICE), 10.0)

    def test_missing_price_is_unpriced_tokens_only(self):
        self.prices.write_text(json.dumps({"b/m": BUILDER_PRICE}), encoding="utf-8")
        report = self.report()
        self.assertEqual(report["mode"], "unpriced")
        self.assertNotIn("savings_pct", report)
        self.assertEqual(report["tokens"]["builder"]["input"], 5000)
        self.assertEqual(report["tokens"]["navigator"]["input"], 1000)

    def test_aliases_resolve(self):
        self.prices.write_text(json.dumps({
            "real/nav": NAV_PRICE, "real/builder": BUILDER_PRICE,
            "aliases": {"nav/m": "real/nav", "b/m": "real/builder"}}), encoding="utf-8")
        report = self.report()
        self.assertEqual(report["mode"], "full")
        self.assertEqual(report["savings_pct"], EXPECTED["savings_pct"])

    def test_history_line_appended(self):
        report = self.report()
        prewalk_savings.append_history(report)
        path = prewalk_savings.history_path()
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["handoff_id"], "h1")
        self.assertEqual(entry["mode"], "full")
        self.assertEqual(entry["pct"], EXPECTED["savings_pct"])


class BuilderOnlyTests(SavingsFixture):
    def setUp(self):
        super().setUp()
        # Navigator usage was not collected, but its model is known.
        (self.run / "usage.json").write_text(json.dumps({
            "navigator": {"usage_status": "not_collected", "models": ["nav/m"]}}),
            encoding="utf-8")

    def test_builder_only_is_labelled_upper_bound(self):
        report = self.report()
        self.assertEqual(report["mode"], "builder_only")
        self.assertIn("upper bound", report["label"])
        self.assertAlmostEqual(report["builder_cost"], EXPECTED_ONLY["builder_cost"])
        self.assertAlmostEqual(report["builder_at_nav_price"], EXPECTED_ONLY["builder_at_nav"])
        self.assertEqual(report["builder_only_savings_pct"], EXPECTED_ONLY["pct"])
        self.assertNotIn("savings_pct", report)


class KimiDiscoveryTests(SavingsFixture):
    """Navigator session discovery against a fake `kimi` binary."""

    def setUp(self):
        super().setUp()
        self.state["backend"] = "kimi-sub"
        (self.run / "state.json").write_text(json.dumps(self.state), encoding="utf-8")
        bin_dir = fakebins.make_bin_dir(self.root, names=("kimi",))
        self.kimi = str(bin_dir / ("kimi.cmd" if os.name == "nt" else "kimi"))
        self.nav_dir = self.root / "nav-session" / "agents" / "main"
        self.nav_dir.mkdir(parents=True)
        self.other_dir = self.root / "other-session" / "agents" / "main"
        self.other_dir.mkdir(parents=True)
        import datetime as _dt
        self.start_ms = int(_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc).timestamp() * 1000)
        self.end_ms = self.start_ms + 3_600_000

    def write_wire(self, directory: Path, records: list[dict], marker: bool) -> Path:
        wire = directory / "wire.jsonl"
        lines = []
        if marker:
            lines.append(json.dumps({"type": "note", "text": f"launch --run-dir {self.run}"}))
        lines += [json.dumps(r) for r in records]
        wire.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return wire

    def session_list(self):
        sessions = [{"id": "builder-sess", "sessionDir": str(self.root / "builder-session")},
                    {"id": "nav-sess", "sessionDir": str(self.root / "nav-session")},
                    {"id": "other-sess", "sessionDir": str(self.root / "other-session")}]
        listing = self.root / "sessions.json"
        listing.write_text(json.dumps(sessions), encoding="utf-8")
        return listing

    def usage(self, input_other=1000, cache_read=500, output=200):
        return {"inputOther": input_other, "inputCacheRead": cache_read,
                "inputCacheCreation": 0, "output": output}

    def test_navigator_session_picked_by_run_dir_marker(self):
        in_window = self.start_ms + 60_000
        self.write_wire(self.nav_dir, [wire_record(in_window, self.usage())], marker=True)
        self.write_wire(self.other_dir, [wire_record(in_window, self.usage(9999))], marker=False)
        with mock.patch.dict(os.environ, {"KIMI_BIN": self.kimi,
                                        "FAKE_KIMI_SESSION_LIST": str(self.session_list())}):
            report = self.report()
        self.assertEqual(report["navigator_usage"], "auto")
        self.assertEqual(report["tokens"]["navigator"], NAV_TOKENS)
        self.assertEqual(report["mode"], "full")
        self.assertEqual(report["savings_pct"], EXPECTED["savings_pct"])

    def test_zero_or_multiple_matches_are_unavailable(self):
        in_window = self.start_ms + 60_000
        self.write_wire(self.nav_dir, [wire_record(in_window, self.usage())], marker=False)
        self.write_wire(self.other_dir, [wire_record(in_window, self.usage(9999))], marker=False)
        with mock.patch.dict(os.environ, {"KIMI_BIN": self.kimi,
                                        "FAKE_KIMI_SESSION_LIST": str(self.session_list())}):
            report = self.report()
        self.assertEqual(report["navigator_usage"], "unavailable")
        self.assertIn("no Navigator session", report["navigator_usage_reason"])
        self.assertEqual(report["mode"], "builder_only")

        # Two sessions referencing the run dir are ambiguous.
        self.write_wire(self.nav_dir, [wire_record(in_window, self.usage())], marker=True)
        self.write_wire(self.other_dir, [wire_record(in_window, self.usage(9999))], marker=True)
        with mock.patch.dict(os.environ, {"KIMI_BIN": self.kimi,
                                        "FAKE_KIMI_SESSION_LIST": str(self.session_list())}):
            report = self.report()
        self.assertEqual(report["navigator_usage"], "unavailable")
        self.assertIn("2 Navigator sessions", report["navigator_usage_reason"])

    def test_time_window_excludes_out_of_window_calls(self):
        self.write_wire(self.nav_dir, [
            wire_record(self.start_ms - 1000, self.usage(99999), extra="before"),
            wire_record(self.start_ms + 1000, self.usage(), extra="inside"),
            wire_record(self.end_ms + 1000, self.usage(99999), extra="after"),
        ], marker=True)
        self.write_wire(self.other_dir, [], marker=False)
        with mock.patch.dict(os.environ, {"KIMI_BIN": self.kimi,
                                        "FAKE_KIMI_SESSION_LIST": str(self.session_list())}):
            report = self.report()
        self.assertEqual(report["tokens"]["navigator"], NAV_TOKENS)
        self.assertEqual(report["navigator_calls"], 1)


if __name__ == "__main__":
    unittest.main()
