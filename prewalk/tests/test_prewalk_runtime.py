from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "prewalk_runtime.py"
SPEC = importlib.util.spec_from_file_location("prewalk_runtime_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
RUNTIME = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNTIME
SPEC.loader.exec_module(RUNTIME)

MISSING = object()
SESSION_ID = "session_test-1"
THREAD_ID = "thread_test-1"


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def kimi_events(
    final: str = "KIMI_FINAL\nPREWALK_PHASE: SOL_REVIEW",
    session_id: str = SESSION_ID,
) -> list[dict]:
    return [
        {"role": "meta", "type": "system.version", "version": "0.43.1"},
        {"role": "assistant", "content": "DRAFT_RESPONSE"},
        {"role": "assistant", "content": final},
        {
            "role": "meta",
            "type": "session.resume_hint",
            "session_id": session_id,
            "command": f"kimi -r {session_id}",
        },
    ]


def kimi_usage(
    input_other: int = 100,
    cache_read: int = 50,
    cache_creation: int = 25,
    output: int = 10,
    *,
    scope: object = MISSING,
    model: str = "deepseek/deepseek-flash",
    time: int = 1000,
    cost: object = MISSING,
) -> dict:
    record = {
        "type": "usage.record",
        "agentId": "main",
        "model": model,
        "usage": {
            "inputOther": input_other,
            "output": output,
            "inputCacheRead": cache_read,
            "inputCacheCreation": cache_creation,
        },
        "time": time,
    }
    if scope is not MISSING:
        record["usageScope"] = scope
    else:
        record["usageScope"] = "turn"
    if cost is not MISSING:
        record["cost"] = cost
    return record


def codex_events(
    final: str = "CODEX_FINAL\nPREWALK_PHASE: SOL_REVIEW",
    thread_id: str = THREAD_ID,
    usage: dict | None = None,
    *,
    cost: object = MISSING,
) -> list[dict]:
    events = [
        {"type": "thread.started", "thread_id": thread_id},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": final},
        },
        {
            "type": "turn.completed",
            "usage": usage
            or {
                "input_tokens": 1000,
                "cached_input_tokens": 800,
                "output_tokens": 50,
                "reasoning_output_tokens": 10,
            },
        },
    ]
    if cost is not MISSING:
        events[-1]["cost"] = cost
    return events


class RuntimeTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.events = self.root / "turn-001-events.jsonl"
        self.events2 = self.root / "turn-002-events.jsonl"
        self.events3 = self.root / "turn-003-events.jsonl"
        self.final = self.root / "turn-001-final.txt"
        self.final2 = self.root / "turn-002-final.txt"
        self.final3 = self.root / "turn-003-final.txt"
        self.usage = self.root / "usage.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_kimi_session(
        self,
        records: list[dict],
        session_id: str = SESSION_ID,
    ) -> list[dict]:
        session_dir = self.root / "session-store" / session_id
        write_jsonl(session_dir / "agents" / "main" / "wire.jsonl", records)
        return [
            {
                "id": session_id,
                "workDir": str(self.root),
                "sessionDir": str(session_dir),
            }
        ]

    def mock_kimi_sessions(self, sessions: list[dict]):
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch.object(RUNTIME, "_find_kimi_binary", return_value="kimi")
        )
        run = stack.enter_context(
            mock.patch.object(
                RUNTIME.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(sessions),
                    stderr="",
                ),
            )
        )
        self.addCleanup(stack.close)
        return run

    def inspect_kimi(
        self,
        events_path: Path,
        final_path: Path,
        builder_turn: int,
        *,
        expected_session: str | None = None,
        usage_path: Path | None = None,
    ) -> dict:
        return RUNTIME.inspect(
            "kimi",
            events_path,
            final_path,
            usage_path or self.usage,
            builder_turn,
            expected_session=expected_session,
            cwd=self.root,
        )

    def inspect_codex(
        self,
        events_path: Path,
        final_path: Path,
        builder_turn: int,
        *,
        expected_session: str | None = None,
        usage_path: Path | None = None,
    ) -> dict:
        return RUNTIME.inspect(
            "codex",
            events_path,
            final_path,
            usage_path or self.usage,
            builder_turn,
            expected_session=expected_session,
        )


class KimiUsageTests(RuntimeTestBase):
    def test_aggregates_multiple_model_calls_into_one_builder_turn(self):
        write_jsonl(self.events, kimi_events())
        records = [
            kimi_usage(100, 50, 25, 10, time=1000),
            kimi_usage(40, 160, 0, 5, time=1100),
            kimi_usage(10, 0, 0, 2, time=1200),
        ]
        sessions = self.write_kimi_session(records)
        run = self.mock_kimi_sessions(sessions)

        summary = self.inspect_kimi(
            self.events,
            self.final,
            1,
            expected_session=SESSION_ID,
        )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["usage_turn_count"], 1)
        self.assertEqual(summary["usage_call_count"], 3)
        self.assertEqual(summary["usage_status"], "available")
        self.assertEqual(summary["cost_status"], "unavailable")
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["schema_version"], 2)
        self.assertEqual(usage["builder_turn"], 1)
        self.assertEqual(usage["turn_count"], 1)
        self.assertEqual(usage["call_count"], 3)
        self.assertEqual(len(usage["turns"]), 1)
        turn = usage["turns"][0]
        self.assertEqual(turn["turn"], 1)
        self.assertEqual(turn["call_count"], 3)
        self.assertEqual(turn["inputOther"], 150)
        self.assertEqual(turn["inputCacheRead"], 210)
        self.assertEqual(turn["inputCacheCreation"], 25)
        self.assertEqual(turn["output"], 17)
        self.assertEqual(turn["input_total"], 385)
        self.assertAlmostEqual(turn["cache_read_ratio"], 210 / 385)
        self.assertEqual(usage["totals"]["inputOther"], 150)
        self.assertEqual(usage["totals"]["input_total"], 385)
        self.assertAlmostEqual(usage["totals"]["cache_read_ratio"], 210 / 385)
        self.assertEqual([call["call"] for call in usage["calls"]], [1, 2, 3])
        self.assertEqual(
            [call["turn_call"] for call in usage["calls"]], [1, 2, 3]
        )
        self.assertEqual(
            {call["builder_turn"] for call in usage["calls"]}, {1}
        )
        self.assertEqual(usage["models"], ["deepseek/deepseek-flash"])
        self.assertIsNone(usage["cost"])
        self.assertEqual(usage["cost_status"], "unavailable")
        self.assertNotIn("DRAFT_RESPONSE", self.usage.read_text(encoding="utf-8"))
        self.assertEqual(
            self.final.read_text(encoding="utf-8"),
            "KIMI_FINAL\nPREWALK_PHASE: SOL_REVIEW",
        )
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["kimi", "session", "list"])
        self.assertIn("--cwd", command)
        self.assertIn("--json", command)

    def test_two_turns_accumulate_and_reinspection_is_idempotent(self):
        write_jsonl(self.events, kimi_events("TURN_ONE"))
        sessions = self.write_kimi_session(
            [
                kimi_usage(100, 50, 25, 10, time=1000),
                kimi_usage(40, 160, 0, 5, time=1100),
            ]
        )
        self.mock_kimi_sessions(sessions)
        self.inspect_kimi(self.events, self.final, 1, expected_session=SESSION_ID)

        write_jsonl(self.events2, kimi_events("TURN_TWO"))
        sessions = self.write_kimi_session(
            [
                kimi_usage(100, 50, 25, 10, time=1000),
                kimi_usage(40, 160, 0, 5, time=1100),
                kimi_usage(10, 0, 0, 2, time=2000),
                kimi_usage(20, 10, 0, 3, time=2100),
                kimi_usage(30, 20, 0, 4, time=2200),
            ]
        )
        self.mock_kimi_sessions(sessions)
        summary = self.inspect_kimi(
            self.events2,
            self.final2,
            2,
            expected_session=SESSION_ID,
        )
        self.assertEqual(summary["usage_turn_count"], 2)
        self.assertEqual(summary["usage_call_count"], 5)
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["builder_turn"], 2)
        self.assertEqual([turn["turn"] for turn in usage["turns"]], [1, 2])
        self.assertEqual([turn["call_count"] for turn in usage["turns"]], [2, 3])
        self.assertEqual(usage["call_count"], 5)
        self.assertEqual(usage["totals"]["inputOther"], 200)
        self.assertEqual(usage["totals"]["output"], 24)
        self.assertEqual(usage["turns"][1]["inputOther"], 60)
        self.assertEqual(usage["turns"][1]["output"], 9)
        self.assertEqual(
            [call["turn_call"] for call in usage["calls"] if call["builder_turn"] == 2],
            [1, 2, 3],
        )
        self.assertEqual(
            [call["builder_turn"] for call in usage["calls"]],
            [1, 1, 2, 2, 2],
        )
        self.assertEqual(
            usage["turns"][1]["source_events"], [str(self.events2)]
        )

        before = self.usage.read_text(encoding="utf-8")
        repeat = self.inspect_kimi(
            self.events2,
            self.final2,
            2,
            expected_session=SESSION_ID,
        )
        self.assertEqual(repeat["usage_turn_count"], 2)
        self.assertEqual(repeat["usage_call_count"], 5)
        self.assertEqual(self.usage.read_text(encoding="utf-8"), before)

        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_kimi(
                self.events,
                self.final,
                1,
                expected_session=SESSION_ID,
            )
        self.assertEqual(self.usage.read_text(encoding="utf-8"), before)

    def test_turn_prompt_segments_split_a_retrospective_wire(self):
        write_jsonl(self.events, kimi_events("ONE"))
        wire_records = [
            {"type": "turn.prompt", "content": "first prompt"},
            kimi_usage(10, 0, 0, 1, time=1000),
            kimi_usage(20, 0, 0, 2, time=1100),
            {"type": "turn.prompt", "content": "resume prompt"},
            kimi_usage(30, 0, 0, 3, time=2000),
        ]
        self.mock_kimi_sessions(self.write_kimi_session(wire_records))

        first = self.inspect_kimi(self.events, self.final, 1, expected_session=SESSION_ID)
        self.assertEqual(first["usage_turn_count"], 1)
        self.assertEqual(first["usage_call_count"], 2)
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["turns"][0]["call_count"], 2)
        self.assertEqual(usage["turns"][0]["output"], 3)

        write_jsonl(self.events2, kimi_events("TWO"))
        second = self.inspect_kimi(
            self.events2,
            self.final2,
            2,
            expected_session=SESSION_ID,
        )
        self.assertEqual(second["usage_turn_count"], 2)
        self.assertEqual(second["usage_call_count"], 3)
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual([turn["call_count"] for turn in usage["turns"]], [2, 1])
        self.assertEqual(usage["turns"][1]["output"], 3)
        before = self.usage.read_text(encoding="utf-8")
        repeat = self.inspect_kimi(
            self.events2,
            self.final2,
            2,
            expected_session=SESSION_ID,
        )
        self.assertEqual(repeat["usage_call_count"], 3)
        self.assertEqual(self.usage.read_text(encoding="utf-8"), before)

    def test_forward_append_of_a_third_turn(self):
        write_jsonl(self.events, kimi_events("ONE"))
        sessions = self.write_kimi_session([kimi_usage(10, 0, 0, 1, time=1000)])
        self.mock_kimi_sessions(sessions)
        self.inspect_kimi(self.events, self.final, 1, expected_session=SESSION_ID)

        write_jsonl(self.events2, kimi_events("TWO"))
        sessions = self.write_kimi_session(
            [
                kimi_usage(10, 0, 0, 1, time=1000),
                kimi_usage(20, 0, 0, 2, time=2000),
            ]
        )
        self.mock_kimi_sessions(sessions)
        self.inspect_kimi(self.events2, self.final2, 2, expected_session=SESSION_ID)

        write_jsonl(self.events3, kimi_events("THREE"))
        sessions = self.write_kimi_session(
            [
                kimi_usage(10, 0, 0, 1, time=1000),
                kimi_usage(20, 0, 0, 2, time=2000),
                kimi_usage(30, 0, 0, 3, time=3000),
            ]
        )
        self.mock_kimi_sessions(sessions)
        summary = self.inspect_kimi(
            self.events3,
            self.final3,
            3,
            expected_session=SESSION_ID,
        )
        self.assertEqual(summary["usage_turn_count"], 3)
        self.assertEqual(summary["usage_call_count"], 3)
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual([turn["call_count"] for turn in usage["turns"]], [1, 1, 1])
        self.assertEqual(usage["turns"][2]["output"], 3)
        self.assertEqual(
            usage["turns"][2]["source_events"], [str(self.events3)]
        )

    def test_mixed_scopes_are_excluded_and_marked_partial(self):
        write_jsonl(self.events, kimi_events())
        records = [
            kimi_usage(100, 50, 0, 10, time=1000),
            kimi_usage(9999, 9999, 9999, 9999, scope="session", time=1100),
            kimi_usage(10, 0, 0, 2, scope=None, time=1200),
        ]
        sessions = self.write_kimi_session(records)
        self.mock_kimi_sessions(sessions)

        summary = self.inspect_kimi(
            self.events,
            self.final,
            1,
            expected_session=SESSION_ID,
        )

        self.assertEqual(summary["usage_status"], "partial")
        self.assertEqual(summary["usage_turn_count"], 1)
        self.assertEqual(summary["usage_call_count"], 2)
        self.assertEqual(summary["skipped_scope_count"], 1)
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["usage_status"], "partial")
        self.assertEqual(usage["skipped_scope_count"], 1)
        self.assertEqual(usage["turns"][0]["call_count"], 2)
        self.assertEqual(usage["totals"]["inputOther"], 110)
        self.assertEqual(usage["totals"]["inputCacheRead"], 50)
        self.assertEqual(usage["totals"]["output"], 12)
        self.assertIn("excluded 1 non-turn", usage["usage_error"])
        self.assertEqual(
            [call["scope"] for call in usage["calls"]], ["turn", None]
        )

    def test_missing_usage_is_unavailable_not_zero(self):
        write_jsonl(self.events, kimi_events())
        sessions = self.write_kimi_session([])
        self.mock_kimi_sessions(sessions)

        summary = self.inspect_kimi(
            self.events,
            self.final,
            1,
            expected_session=SESSION_ID,
        )

        self.assertEqual(summary["usage_status"], "unavailable")
        self.assertEqual(summary["usage_turn_count"], 1)
        self.assertEqual(summary["usage_call_count"], 0)
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["usage_status"], "unavailable")
        self.assertEqual(usage["call_count"], 0)
        self.assertEqual(usage["turn_count"], 1)
        self.assertEqual(usage["turns"][0]["call_count"], 0)
        self.assertIsNone(usage["totals"])
        self.assertIsNone(usage["cost"])
        self.assertEqual(usage["cost_status"], "unavailable")
        self.assertIn("no usage.record entries", usage["usage_error"])
        self.assertEqual(
            self.final.read_text(encoding="utf-8"),
            "KIMI_FINAL\nPREWALK_PHASE: SOL_REVIEW",
        )

    def test_session_not_found_marks_usage_unavailable(self):
        write_jsonl(self.events, kimi_events())
        self.mock_kimi_sessions([])
        summary = self.inspect_kimi(
            self.events,
            self.final,
            1,
            expected_session=SESSION_ID,
        )
        self.assertEqual(summary["usage_status"], "unavailable")
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertIn("not found", usage["usage_error"])
        self.assertEqual(self.final.read_text(encoding="utf-8"), "KIMI_FINAL\nPREWALK_PHASE: SOL_REVIEW")


class KimiContinuityTests(RuntimeTestBase):
    def test_v1_prior_hint_slices_the_first_builder_turn(self):
        write_jsonl(self.events, kimi_events())
        sessions = self.write_kimi_session(
            [
                kimi_usage(10, 0, 0, 1, time=1000),
                kimi_usage(20, 0, 0, 2, time=1100),
                kimi_usage(30, 0, 0, 3, time=1200),
            ]
        )
        self.mock_kimi_sessions(sessions)
        self.usage.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "host": "kimi",
                    "session_id": SESSION_ID,
                    "turns": [{"turn": 1}, {"turn": 2}],
                }
            ),
            encoding="utf-8",
        )

        self.inspect_kimi(self.events, self.final, 1, expected_session=SESSION_ID)

        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["schema_version"], 2)
        self.assertEqual(usage["turn_count"], 1)
        self.assertEqual(usage["call_count"], 2)
        self.assertEqual(usage["turns"][0]["output"], 3)
        self.assertEqual(usage["turns"][0]["source_events"], [str(self.events)])

    def test_v1_prior_is_rejected_for_a_resumed_turn(self):
        write_jsonl(self.events2, kimi_events("TWO"))
        self.usage.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "host": "kimi",
                    "session_id": SESSION_ID,
                    "turns": [{"turn": 1}],
                }
            ),
            encoding="utf-8",
        )
        before = self.usage.read_text(encoding="utf-8")
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_kimi(
                self.events2,
                self.final2,
                2,
                expected_session=SESSION_ID,
            )
        self.assertEqual(self.usage.read_text(encoding="utf-8"), before)
        self.assertFalse(self.final2.exists())

    def test_schema_and_session_mismatches_do_not_overwrite(self):
        write_jsonl(self.events, kimi_events())
        self.mock_kimi_sessions(self.write_kimi_session([kimi_usage()]))
        cases = [
            {
                "schema_version": 2,
                "host": "codex",
                "session_id": SESSION_ID,
                "turns": [],
                "calls": [],
            },
            {
                "schema_version": 2,
                "host": "kimi",
                "session_id": "session_other",
                "turns": [],
                "calls": [],
            },
            {
                "schema_version": 2,
                "host": "kimi",
                "session_id": SESSION_ID,
                "turns": [
                    {"turn": 1, "call_count": 0, "source_events": []},
                    {"turn": 3, "call_count": 0, "source_events": []},
                ],
                "calls": [],
            },
        ]
        for index, prior in enumerate(cases):
            with self.subTest(index=index):
                path = self.root / f"usage-mismatch-{index}.json"
                path.write_text(json.dumps(prior), encoding="utf-8")
                before = path.read_text(encoding="utf-8")
                with self.assertRaises(RUNTIME.InspectError):
                    self.inspect_kimi(
                        self.events,
                        self.root / f"final-mismatch-{index}.txt",
                        1,
                        expected_session=SESSION_ID,
                        usage_path=path,
                    )
                self.assertEqual(path.read_text(encoding="utf-8"), before)
                self.assertFalse((self.root / f"final-mismatch-{index}.txt").exists())

    def test_expected_session_mismatch_does_not_overwrite(self):
        write_jsonl(self.events, kimi_events())
        self.final.write_text("keep-final", encoding="utf-8")
        self.usage.write_text("keep-usage", encoding="utf-8")
        self.mock_kimi_sessions(self.write_kimi_session([kimi_usage()]))
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_kimi(
                self.events,
                self.final,
                1,
                expected_session="session_other",
            )
        self.assertEqual(self.final.read_text(encoding="utf-8"), "keep-final")
        self.assertEqual(self.usage.read_text(encoding="utf-8"), "keep-usage")

    def test_resumed_turn_requires_expected_session_and_prior_usage(self):
        write_jsonl(self.events2, kimi_events("TWO", SESSION_ID))
        self.final2.write_text("keep-final", encoding="utf-8")
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_kimi(self.events2, self.final2, 2)
        self.assertEqual(self.final2.read_text(encoding="utf-8"), "keep-final")
        self.assertFalse(self.usage.exists())

        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_kimi(
                self.events2,
                self.final2,
                2,
                expected_session=SESSION_ID,
            )
        self.assertEqual(self.final2.read_text(encoding="utf-8"), "keep-final")
        self.assertFalse(self.usage.exists())

    def test_wire_shorter_than_prior_is_a_non_overwrite_failure(self):
        write_jsonl(self.events, kimi_events())
        self.mock_kimi_sessions(self.write_kimi_session([kimi_usage()]))
        prior = {
            "schema_version": 2,
            "host": "kimi",
            "session_id": SESSION_ID,
            "turns": [
                {"turn": 1, "call_count": 2, "source_events": [str(self.events)]}
            ],
            "calls": [
                {"builder_turn": 1, "usage": {"inputOther": 1, "output": 1}},
                {"builder_turn": 1, "usage": {"inputOther": 2, "output": 2}},
            ],
            "skipped_scope_count": 0,
        }
        self.usage.write_text(json.dumps(prior), encoding="utf-8")
        before = self.usage.read_text(encoding="utf-8")
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_kimi(
                self.events,
                self.final,
                1,
                expected_session=SESSION_ID,
            )
        self.assertEqual(self.usage.read_text(encoding="utf-8"), before)
        self.assertFalse(self.final.exists())

    def test_kimi_bin_cmd_wrapping(self):
        cmd_path = self.root / "kimi.cmd"
        cmd_path.write_text("@echo off\n", encoding="utf-8")
        write_jsonl(self.events, kimi_events())
        sessions = self.write_kimi_session([kimi_usage()])
        with mock.patch.dict(os.environ, {"KIMI_BIN": str(cmd_path)}, clear=False):
            with mock.patch.object(RUNTIME.shutil, "which", return_value=None):
                with mock.patch.object(
                    RUNTIME.subprocess,
                    "run",
                    return_value=SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps(sessions),
                        stderr="",
                    ),
                ) as run:
                    summary = self.inspect_kimi(
                        self.events,
                        self.final,
                        1,
                        expected_session=SESSION_ID,
                    )
        self.assertEqual(summary["usage_call_count"], 1)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["cmd", "/c", str(cmd_path.resolve())])
        self.assertEqual(command[3:5], ["session", "list"])
        self.assertEqual(
            RUNTIME._kimi_argv("C:/tools/kimi.bat", ["session"]),
            ["cmd", "/c", "C:/tools/kimi.bat", "session"],
        )
        self.assertEqual(
            RUNTIME._kimi_argv("C:/tools/kimi.exe", ["session"]),
            ["C:/tools/kimi.exe", "session"],
        )


class CostTests(RuntimeTestBase):
    def test_kimi_valid_and_invalid_cost_values(self):
        write_jsonl(self.events, kimi_events())
        sessions = self.write_kimi_session(
            [
                kimi_usage(10, 0, 0, 1, cost=0.012),
                kimi_usage(20, 0, 0, 2, cost={"amount": 0.5, "currency": "USD"}),
            ]
        )
        self.mock_kimi_sessions(sessions)
        self.inspect_kimi(self.events, self.final, 1, expected_session=SESSION_ID)
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["cost_status"], "reported")
        self.assertEqual(
            usage["cost"],
            [
                {"builder_turn": 1, "call": 1, "value": 0.012},
                {
                    "builder_turn": 1,
                    "call": 2,
                    "value": {"amount": 0.5, "currency": "USD"},
                },
            ],
        )

        for index, invalid in enumerate(("free", {"amount": "0.5"}, {"amount": 0.5, "currency": 3})):
            with self.subTest(index=index):
                path = self.root / f"usage-invalid-cost-{index}.json"
                events = self.root / f"events-invalid-cost-{index}.jsonl"
                final = self.root / f"final-invalid-cost-{index}.txt"
                write_jsonl(events, kimi_events())
                sessions = self.write_kimi_session(
                    [kimi_usage(10, 0, 0, 1, cost=invalid)]
                )
                self.mock_kimi_sessions(sessions)
                self.inspect_kimi(
                    events,
                    final,
                    1,
                    expected_session=SESSION_ID,
                    usage_path=path,
                )
                usage = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(usage["cost_status"], "unavailable")
                self.assertIsNone(usage["cost"])

    def test_codex_valid_and_invalid_cost_values(self):
        valid_cases = [
            (0.02, 0.02),
            ({"amount": 0.03, "currency": "USD"}, {"amount": 0.03, "currency": "USD"}),
        ]
        for index, (cost, expected) in enumerate(valid_cases):
            with self.subTest(index=index):
                events = self.root / f"codex-cost-events-{index}.jsonl"
                final = self.root / f"codex-cost-final-{index}.txt"
                usage_path = self.root / f"codex-cost-usage-{index}.json"
                write_jsonl(events, codex_events(cost=cost))
                self.inspect_codex(
                    events,
                    final,
                    1,
                    expected_session=THREAD_ID,
                    usage_path=usage_path,
                )
                usage = json.loads(usage_path.read_text(encoding="utf-8"))
                self.assertEqual(usage["cost_status"], "reported")
                self.assertEqual(usage["cost"], expected)

        for index, cost in enumerate(("free", {"amount": "0.03"})):
            with self.subTest(index=f"invalid-{index}"):
                events = self.root / f"codex-bad-cost-events-{index}.jsonl"
                final = self.root / f"codex-bad-cost-final-{index}.txt"
                usage_path = self.root / f"codex-bad-cost-usage-{index}.json"
                write_jsonl(events, codex_events(cost=cost))
                self.inspect_codex(
                    events,
                    final,
                    1,
                    expected_session=THREAD_ID,
                    usage_path=usage_path,
                )
                usage = json.loads(usage_path.read_text(encoding="utf-8"))
                self.assertEqual(usage["cost_status"], "unavailable")
                self.assertIsNone(usage["cost"])


class CodexUsageTests(RuntimeTestBase):
    def test_two_turns_accumulate_and_reinspection_is_idempotent(self):
        write_jsonl(
            self.events,
            codex_events(
                "TURN_ONE",
                usage={
                    "input_tokens": 1000,
                    "cached_input_tokens": 800,
                    "output_tokens": 50,
                    "reasoning_output_tokens": 10,
                },
            ),
        )
        first = self.inspect_codex(
            self.events,
            self.final,
            1,
            expected_session=THREAD_ID,
        )
        self.assertEqual(first["usage_turn_count"], 1)
        self.assertEqual(first["usage_call_count"], 1)
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["schema_version"], 2)
        self.assertEqual(usage["cache_ratio_denominator"], "input_tokens")
        self.assertAlmostEqual(usage["totals"]["cache_read_ratio"], 0.8)

        write_jsonl(
            self.events2,
            codex_events(
                "TURN_TWO",
                usage={
                    "input_tokens": 500,
                    "cached_input_tokens": 100,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 5,
                },
            ),
        )
        second = self.inspect_codex(
            self.events2,
            self.final2,
            2,
            expected_session=THREAD_ID,
        )
        self.assertEqual(second["usage_turn_count"], 2)
        self.assertEqual(second["usage_call_count"], 2)
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual([turn["call_count"] for turn in usage["turns"]], [1, 1])
        self.assertEqual(usage["totals"]["input_tokens"], 1500)
        self.assertEqual(usage["totals"]["cached_input_tokens"], 900)
        self.assertEqual(usage["totals"]["output_tokens"], 70)
        self.assertEqual(usage["totals"]["reasoning_output_tokens"], 15)
        self.assertAlmostEqual(usage["totals"]["cache_read_ratio"], 0.6)
        self.assertAlmostEqual(usage["turns"][0]["cache_read_ratio"], 0.8)
        self.assertAlmostEqual(usage["turns"][1]["cache_read_ratio"], 0.2)
        self.assertEqual(
            usage["turns"][0]["source_events"], [str(self.events)]
        )
        self.assertEqual(
            usage["turns"][1]["source_events"], [str(self.events2)]
        )

        before = self.usage.read_text(encoding="utf-8")
        repeat = self.inspect_codex(
            self.events2,
            self.final2,
            2,
            expected_session=THREAD_ID,
        )
        self.assertEqual(repeat["usage_call_count"], 2)
        self.assertEqual(self.usage.read_text(encoding="utf-8"), before)

        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_codex(
                self.events,
                self.final,
                1,
                expected_session=THREAD_ID,
            )
        self.assertEqual(self.usage.read_text(encoding="utf-8"), before)

    def test_missing_usage_is_unavailable_not_zero(self):
        write_jsonl(
            self.events,
            [event for event in codex_events() if event["type"] != "turn.completed"],
        )
        summary = self.inspect_codex(
            self.events,
            self.final,
            1,
            expected_session=THREAD_ID,
        )
        self.assertEqual(summary["usage_status"], "unavailable")
        self.assertEqual(summary["usage_turn_count"], 1)
        self.assertEqual(summary["usage_call_count"], 0)
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["turn_count"], 1)
        self.assertEqual(usage["turns"][0]["call_count"], 0)
        self.assertIsNone(usage["totals"])
        self.assertEqual(usage["cost_status"], "unavailable")

    def test_resumed_turn_requires_prior_usage(self):
        write_jsonl(self.events2, codex_events("TWO"))
        self.final2.write_text("keep-final", encoding="utf-8")
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_codex(
                self.events2,
                self.final2,
                2,
                expected_session=THREAD_ID,
            )
        self.assertEqual(self.final2.read_text(encoding="utf-8"), "keep-final")
        self.assertFalse(self.usage.exists())

    def test_expected_session_mismatch_does_not_overwrite(self):
        write_jsonl(self.events, codex_events())
        self.final.write_text("keep-final", encoding="utf-8")
        self.usage.write_text("keep-usage", encoding="utf-8")
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_codex(
                self.events,
                self.final,
                1,
                expected_session="thread_other",
            )
        self.assertEqual(self.final.read_text(encoding="utf-8"), "keep-final")
        self.assertEqual(self.usage.read_text(encoding="utf-8"), "keep-usage")


class MalformedInputTests(RuntimeTestBase):
    def test_malformed_jsonl_reports_line_number_without_writing_outputs(self):
        self.events.write_text(
            '{"type":"thread.started","thread_id":"thread_1"}\n'
            "{not-json}\n",
            encoding="utf-8",
        )
        with self.assertRaises(RUNTIME.InspectError) as caught:
            self.inspect_codex(self.events, self.final, 1)
        self.assertIn("line 2", str(caught.exception))
        self.assertFalse(self.final.exists())
        self.assertFalse(self.usage.exists())

    def test_missing_session_or_final_message_is_an_error(self):
        write_jsonl(self.events, [{"role": "assistant", "content": "NO_SESSION"}])
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_kimi(self.events, self.final, 1)
        self.assertFalse(self.final.exists())

        write_jsonl(
            self.events,
            [
                {
                    "role": "meta",
                    "type": "session.resume_hint",
                    "session_id": "session_only",
                }
            ],
        )
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_kimi(self.events, self.final, 1)
        self.assertFalse(self.final.exists())

    def test_missing_codex_thread_is_an_error(self):
        write_jsonl(self.events, codex_events()[1:])
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_codex(self.events, self.final, 1)
        self.assertFalse(self.final.exists())

    def test_malformed_kimi_wire_marks_usage_unavailable_with_line(self):
        write_jsonl(self.events, kimi_events())
        session_dir = self.root / "session-store" / SESSION_ID
        wire = session_dir / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True, exist_ok=True)
        wire.write_text('{"type":"usage.record"}\n{not-json}\n', encoding="utf-8")
        sessions = [
            {
                "id": SESSION_ID,
                "workDir": str(self.root),
                "sessionDir": str(session_dir),
            }
        ]
        self.mock_kimi_sessions(sessions)
        summary = self.inspect_kimi(
            self.events,
            self.final,
            1,
            expected_session=SESSION_ID,
        )
        self.assertEqual(summary["usage_status"], "unavailable")
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertIn("line 2", usage["usage_error"])
        self.assertEqual(self.final.read_text(encoding="utf-8"), "KIMI_FINAL\nPREWALK_PHASE: SOL_REVIEW")


class CliAndSafetyTests(RuntimeTestBase):
    def test_builder_turn_is_required_and_positive(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                RUNTIME.main(
                    [
                        "inspect",
                        "--host",
                        "codex",
                        "--events",
                        str(self.events),
                        "--final-message",
                        str(self.final),
                        "--usage",
                        str(self.usage),
                    ]
                )
        self.assertEqual(caught.exception.code, 2)

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                RUNTIME.main(
                    [
                        "inspect",
                        "--host",
                        "codex",
                        "--builder-turn",
                        "0",
                        "--events",
                        str(self.events),
                        "--final-message",
                        str(self.final),
                        "--usage",
                        str(self.usage),
                    ]
                )
        self.assertEqual(caught.exception.code, 2)

    def test_cli_returns_nonzero_for_resume_without_expected_session(self):
        write_jsonl(self.events2, kimi_events("TWO"))
        self.final2.write_text("keep-final", encoding="utf-8")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = RUNTIME.main(
                [
                    "inspect",
                    "--host",
                    "kimi",
                    "--builder-turn",
                    "2",
                    "--events",
                    str(self.events2),
                    "--final-message",
                    str(self.final2),
                    "--usage",
                    str(self.usage),
                    "--cwd",
                    str(self.root),
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("expected-session", stderr.getvalue())
        self.assertEqual(self.final2.read_text(encoding="utf-8"), "keep-final")
        self.assertFalse(self.usage.exists())

    def test_stdout_summary_omits_conversation_and_prompt(self):
        write_jsonl(self.events, kimi_events())
        sessions = self.write_kimi_session(
            [
                kimi_usage(10, 0, 0, 1, time=1000),
                kimi_usage(20, 0, 0, 2, time=1100),
            ]
        )
        self.mock_kimi_sessions(sessions)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = RUNTIME.main(
                [
                    "inspect",
                    "--host",
                    "kimi",
                    "--builder-turn",
                    "1",
                    "--events",
                    str(self.events),
                    "--expected-session",
                    SESSION_ID,
                    "--final-message",
                    str(self.final),
                    "--usage",
                    str(self.usage),
                    "--cwd",
                    str(self.root),
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["session_id"], SESSION_ID)
        self.assertEqual(payload["builder_turn"], 1)
        self.assertEqual(payload["usage_turn_count"], 1)
        self.assertEqual(payload["usage_call_count"], 2)
        self.assertEqual(payload["final_message"], str(self.final))
        self.assertEqual(payload["usage"], str(self.usage))
        self.assertNotIn("KIMI_FINAL", stdout.getvalue())
        self.assertNotIn("DRAFT_RESPONSE", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
