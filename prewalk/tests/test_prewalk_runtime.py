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
CLAUDE_SESSION_ID = "89f0946a-3614-4288-b519-e53c1dd89c60"
DSH_SESSION_ID = "dsh-session-1"
GENERIC_SESSION_ID = "generic-session-1"


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


def claude_usage(
    input_tokens: int = 1000,
    cache_read: int = 800,
    cache_creation: int = 100,
    output: int = 50,
) -> dict:
    return {
        "input_tokens": input_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "output_tokens": output,
    }


def claude_result(
    final: str = "CLAUDE_FINAL\nPREWALK_PHASE: NAVIGATOR_REVIEW",
    session_id: str = CLAUDE_SESSION_ID,
    usage: object = MISSING,
    *,
    cost: object = MISSING,
    is_error: bool = False,
    model: str | None = "claude-sonnet-4-5",
) -> dict:
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "result": final,
        "session_id": session_id,
        "modelUsage": {model: {"inputTokens": 1}} if model else {},
    }
    if usage is not MISSING:
        result["usage"] = usage
    if cost is not MISSING:
        result["total_cost_usd"] = cost
    return result


def claude_events(
    final: str = "CLAUDE_FINAL\nPREWALK_PHASE: NAVIGATOR_REVIEW",
    session_id: str = CLAUDE_SESSION_ID,
    usage: object = MISSING,
    *,
    cost: object = MISSING,
    is_error: bool = False,
    include_init: bool = True,
) -> list[dict]:
    events: list[dict] = []
    if include_init:
        events.append(
            {"type": "system", "subtype": "init", "session_id": session_id}
        )
    events.append(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "DRAFT_RESPONSE"}]
            },
        }
    )
    events.append(
        claude_result(
            final,
            session_id,
            usage,
            cost=cost,
            is_error=is_error,
        )
    )
    return events


def dsh_events(
    final: str = "DSH_FINAL\nPREWALK_PHASE: NAVIGATOR_REVIEW",
    session_id: str = DSH_SESSION_ID,
    *,
    reason: object = "completed",
    text: object = MISSING,
    include_session: bool = True,
    final_id: object = MISSING,
    extra: list[dict] | None = None,
) -> list[dict]:
    events: list[dict] = []
    if include_session:
        events.append(
            {"type": "session", "sessionId": session_id, "profile": "headless"}
        )
    if extra:
        events.extend(extra)
    final_event: dict = {"type": "final"}
    observed_id = session_id if final_id is MISSING else final_id
    if observed_id is not None:
        final_event["sessionId"] = observed_id
    if reason is not MISSING:
        final_event["reason"] = {"kind": reason}
    if text is MISSING:
        final_event["text"] = final
    elif text is not None:
        final_event["text"] = text
    events.append(final_event)
    return events


def generic_events(
    session_id: str = GENERIC_SESSION_ID,
    usages: list[dict] | None = None,
    final: str | None = "GENERIC_FINAL\nPREWALK_PHASE: NAVIGATOR_REVIEW",
    *,
    completed: object = True,
    include_session: bool = True,
    final_id: object = MISSING,
) -> list[dict]:
    events: list[dict] = []
    if include_session:
        events.append({"type": "session", "session_id": session_id})
    if usages:
        events.extend(usages)
    final_event: dict = {"type": "final"}
    observed_id = session_id if final_id is MISSING else final_id
    if observed_id is not None:
        final_event["session_id"] = observed_id
    if completed is not MISSING:
        final_event["completed"] = completed
    if final is not None:
        final_event["text"] = final
    events.append(final_event)
    return events


def generic_usage(
    usage: dict,
    *,
    cost: object = MISSING,
    cost_status: object = MISSING,
    cost_scope: object = MISSING,
    scope: object = MISSING,
    model: str | None = None,
) -> dict:
    event: dict = {"type": "usage", "usage": usage}
    if model is not None:
        event["model"] = model
    if scope is not MISSING:
        event["scope"] = scope
    if cost is not MISSING:
        event["cost"] = cost
    if cost_status is not MISSING:
        event["cost_status"] = cost_status
    if cost_scope is not MISSING:
        event["cost_scope"] = cost_scope
    return event


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

    def inspect_host(
        self,
        host: str,
        events_path: Path,
        final_path: Path,
        builder_turn: int,
        *,
        expected_session: str | None = None,
        usage_path: Path | None = None,
    ) -> dict:
        return RUNTIME.inspect(
            host,
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


class ClaudeCodeTests(RuntimeTestBase):
    def test_initial_turn_extracts_final_usage_and_estimated_cost(self):
        write_jsonl(
            self.events,
            claude_events(usage=claude_usage(), cost=0.25),
        )

        summary = self.inspect_host(
            "claude-code",
            self.events,
            self.final,
            1,
            expected_session=CLAUDE_SESSION_ID,
        )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["usage_turn_count"], 1)
        self.assertEqual(summary["usage_call_count"], 1)
        self.assertEqual(summary["usage_status"], "available")
        self.assertEqual(summary["cost_status"], "estimated")
        self.assertEqual(summary["cost_scope"], "session_cumulative")
        self.assertEqual(
            summary["cache_ratio_denominator"],
            "input_tokens+cache_read_input_tokens+cache_creation_input_tokens",
        )
        self.assertAlmostEqual(summary["cache_read_ratio"], 800 / 1900)

        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["schema_version"], 2)
        self.assertEqual(usage["host"], "claude-code")
        self.assertEqual(usage["session_id"], CLAUDE_SESSION_ID)
        self.assertEqual(usage["call_count"], 1)
        self.assertEqual(usage["turn_count"], 1)
        self.assertEqual(usage["models"], ["claude-sonnet-4-5"])
        self.assertEqual(usage["totals"]["input_tokens"], 1000)
        self.assertEqual(usage["totals"]["cache_read_input_tokens"], 800)
        self.assertEqual(usage["totals"]["cache_creation_input_tokens"], 100)
        self.assertEqual(usage["totals"]["output_tokens"], 50)
        self.assertEqual(usage["totals"]["input_total"], 1900)
        self.assertAlmostEqual(usage["totals"]["cache_read_ratio"], 800 / 1900)
        call = usage["calls"][0]
        self.assertEqual(call["builder_turn"], 1)
        self.assertEqual(call["cost"], 0.25)
        self.assertEqual(call["cost_status"], "estimated")
        self.assertEqual(call["cost_scope"], "session_cumulative")
        self.assertAlmostEqual(call["cache_read_ratio"], 800 / 1900)
        self.assertEqual(usage["cost"], 0.25)
        self.assertEqual(usage["cost_status"], "estimated")
        self.assertEqual(usage["cost_scope"], "session_cumulative")
        self.assertEqual(
            self.final.read_text(encoding="utf-8"),
            "CLAUDE_FINAL\nPREWALK_PHASE: NAVIGATOR_REVIEW",
        )
        self.assertNotIn("DRAFT_RESPONSE", self.usage.read_text(encoding="utf-8"))

    def test_resumed_turns_retain_latest_cumulative_cost_without_summing(self):
        write_jsonl(
            self.events,
            claude_events(
                "TURN_ONE",
                usage=claude_usage(100, 50, 10, 10),
                cost=0.20,
            ),
        )
        self.inspect_host(
            "claude-code",
            self.events,
            self.final,
            1,
            expected_session=CLAUDE_SESSION_ID,
        )

        write_jsonl(
            self.events2,
            claude_events(
                "TURN_TWO",
                usage=claude_usage(200, 100, 20, 20),
                cost=0.35,
            ),
        )
        summary = self.inspect_host(
            "claude-code",
            self.events2,
            self.final2,
            2,
            expected_session=CLAUDE_SESSION_ID,
        )
        self.assertEqual(summary["usage_turn_count"], 2)
        self.assertEqual(summary["usage_call_count"], 2)

        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual([turn["turn"] for turn in usage["turns"]], [1, 2])
        self.assertEqual([turn["call_count"] for turn in usage["turns"]], [1, 1])
        self.assertEqual(usage["totals"]["input_tokens"], 300)
        self.assertEqual(usage["totals"]["cache_read_input_tokens"], 150)
        self.assertEqual(usage["totals"]["input_total"], 480)
        self.assertAlmostEqual(usage["totals"]["cache_read_ratio"], 150 / 480)
        self.assertEqual([call["cost"] for call in usage["calls"]], [0.20, 0.35])
        self.assertEqual(
            [call["cost_scope"] for call in usage["calls"]],
            ["session_cumulative", "session_cumulative"],
        )
        self.assertEqual(usage["cost"], 0.35)
        self.assertEqual(usage["cost_status"], "estimated")
        self.assertEqual(usage["cost_scope"], "session_cumulative")
        self.assertEqual(
            usage["turns"][1]["source_events"], [str(self.events2)]
        )

        before = self.usage.read_text(encoding="utf-8")
        repeat = self.inspect_host(
            "claude-code",
            self.events2,
            self.final2,
            2,
            expected_session=CLAUDE_SESSION_ID,
        )
        self.assertEqual(repeat["usage_call_count"], 2)
        self.assertEqual(self.usage.read_text(encoding="utf-8"), before)

        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_host(
                "claude-code",
                self.events,
                self.final,
                1,
                expected_session=CLAUDE_SESSION_ID,
            )
        self.assertEqual(self.usage.read_text(encoding="utf-8"), before)

    def test_observed_not_logged_in_result_is_rejected_without_outputs(self):
        probe = {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "api_error_status": None,
            "duration_ms": 103,
            "num_turns": 1,
            "result": "Not logged in \u00b7 Please run /login",
            "session_id": CLAUDE_SESSION_ID,
            "total_cost_usd": 0,
            "usage": {
                "input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 0,
            },
            "modelUsage": {},
            "permission_denials": [],
            "terminal_reason": "completed",
        }
        write_jsonl(self.events, [probe])

        with self.assertRaises(RUNTIME.InspectError) as caught:
            self.inspect_host(
                "claude-code",
                self.events,
                self.final,
                1,
                expected_session=CLAUDE_SESSION_ID,
            )
        self.assertIn("is_error=True", str(caught.exception))
        self.assertIn("Not logged in", str(caught.exception))
        self.assertFalse(self.final.exists())
        self.assertFalse(self.usage.exists())

    def test_missing_usage_is_unavailable_not_zero(self):
        write_jsonl(self.events, claude_events())

        summary = self.inspect_host(
            "claude-code",
            self.events,
            self.final,
            1,
            expected_session=CLAUDE_SESSION_ID,
        )

        self.assertEqual(summary["usage_status"], "unavailable")
        self.assertEqual(summary["usage_call_count"], 0)
        self.assertEqual(summary["cost_status"], "unavailable")
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["call_count"], 0)
        self.assertEqual(usage["turn_count"], 1)
        self.assertIsNone(usage["totals"])
        self.assertIsNone(usage["cost"])
        self.assertEqual(usage["cost_status"], "unavailable")
        self.assertIn("no Claude Code usage", usage["usage_error"])
        self.assertEqual(
            self.final.read_text(encoding="utf-8"),
            "CLAUDE_FINAL\nPREWALK_PHASE: NAVIGATOR_REVIEW",
        )

    def test_null_cost_is_unavailable_without_marking_usage_partial(self):
        write_jsonl(
            self.events,
            claude_events(usage=claude_usage(), cost=None),
        )
        summary = self.inspect_host(
            "claude-code",
            self.events,
            self.final,
            1,
            expected_session=CLAUDE_SESSION_ID,
        )
        self.assertEqual(summary["usage_status"], "available")
        self.assertEqual(summary["cost_status"], "unavailable")
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertIsNone(usage["cost"])
        self.assertEqual(usage["cost_status"], "unavailable")
        self.assertIsNone(usage["usage_error"])

    def test_cost_evidence_survives_absent_empty_and_non_object_usage(self):
        cases = {
            "absent": MISSING,
            "empty": {},
            "non_object": "bad",
        }
        for name, raw_usage in cases.items():
            with self.subTest(name=name):
                events_path = self.root / f"claude-cost-{name}.jsonl"
                final_path = self.root / f"claude-cost-{name}.txt"
                usage_path = self.root / f"claude-cost-{name}.json"
                write_jsonl(
                    events_path,
                    claude_events(usage=raw_usage, cost=0.12),
                )
                summary = self.inspect_host(
                    "claude-code",
                    events_path,
                    final_path,
                    1,
                    expected_session=CLAUDE_SESSION_ID,
                    usage_path=usage_path,
                )
                self.assertEqual(summary["usage_call_count"], 1)
                self.assertEqual(summary["usage_status"], "partial")
                self.assertEqual(summary["cost_status"], "estimated")
                self.assertEqual(summary["cost_scope"], "session_cumulative")
                usage = json.loads(usage_path.read_text(encoding="utf-8"))
                self.assertEqual(usage["cost"], 0.12)
                self.assertEqual(usage["cost_status"], "estimated")
                self.assertEqual(usage["cost_scope"], "session_cumulative")
                self.assertEqual(usage["calls"][0]["usage"], {})
                self.assertEqual(
                    usage["calls"][0]["cost_scope"], "session_cumulative"
                )
                self.assertIsNone(usage["totals"])
                self.assertIn("token usage", usage["usage_error"])
                self.assertTrue(final_path.is_file())

    def test_cost_only_resume_is_idempotent_and_keeps_latest_cumulative(self):
        write_jsonl(
            self.events,
            claude_events("TURN_ONE", usage=MISSING, cost=0.10),
        )
        self.inspect_host(
            "claude-code",
            self.events,
            self.final,
            1,
            expected_session=CLAUDE_SESSION_ID,
        )

        write_jsonl(
            self.events2,
            claude_events("TURN_TWO", usage=MISSING, cost=0.18),
        )
        summary = self.inspect_host(
            "claude-code",
            self.events2,
            self.final2,
            2,
            expected_session=CLAUDE_SESSION_ID,
        )
        self.assertEqual(summary["usage_turn_count"], 2)
        self.assertEqual(summary["usage_call_count"], 2)
        self.assertEqual(summary["usage_status"], "partial")
        self.assertEqual(summary["cost_status"], "estimated")
        self.assertEqual(summary["cost_scope"], "session_cumulative")

        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual([call["cost"] for call in usage["calls"]], [0.10, 0.18])
        self.assertEqual(
            [call["usage"] for call in usage["calls"]], [{}, {}]
        )
        self.assertEqual(usage["cost"], 0.18)
        self.assertNotEqual(usage["cost"], 0.28)
        self.assertIsNone(usage["totals"])
        self.assertEqual(
            [turn["call_count"] for turn in usage["turns"]], [1, 1]
        )

        before = self.usage.read_text(encoding="utf-8")
        repeat = self.inspect_host(
            "claude-code",
            self.events2,
            self.final2,
            2,
            expected_session=CLAUDE_SESSION_ID,
        )
        self.assertEqual(repeat["usage_call_count"], 2)
        self.assertEqual(self.usage.read_text(encoding="utf-8"), before)

        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_host(
                "claude-code",
                self.events,
                self.final,
                1,
                expected_session=CLAUDE_SESSION_ID,
            )
        self.assertEqual(self.usage.read_text(encoding="utf-8"), before)

    def test_session_conflicts_and_missing_result_are_errors(self):
        write_jsonl(
            self.events,
            [
                {"type": "system", "subtype": "init", "session_id": "session_a"},
                claude_result(session_id="session_b"),
            ],
        )
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_host("claude-code", self.events, self.final, 1)
        self.assertFalse(self.final.exists())

        write_jsonl(
            self.events,
            [{"type": "assistant", "message": {"content": "no result"}}],
        )
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_host("claude-code", self.events, self.final, 1)

        result = claude_result()
        del result["session_id"]
        write_jsonl(self.events, [result])
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_host("claude-code", self.events, self.final, 1)

        result = claude_result()
        result["session_id"] = 5
        write_jsonl(self.events, [result])
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_host("claude-code", self.events, self.final, 1)

    def test_invalid_cost_and_usage_fields_are_marked_not_invented(self):
        write_jsonl(
            self.events,
            claude_events(usage=claude_usage(), cost="free"),
        )
        summary = self.inspect_host(
            "claude-code",
            self.events,
            self.final,
            1,
            expected_session=CLAUDE_SESSION_ID,
        )
        self.assertEqual(summary["usage_status"], "partial")
        self.assertEqual(summary["cost_status"], "unavailable")
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertIsNone(usage["cost"])
        self.assertEqual(usage["cost_status"], "unavailable")
        self.assertIn("total_cost_usd", usage["usage_error"])

        broken = claude_usage()
        broken["input_tokens"] = -5
        broken["output_tokens"] = "many"
        write_jsonl(
            self.events,
            claude_events(usage=broken, cost=0.1),
        )
        summary = self.inspect_host(
            "claude-code",
            self.events,
            self.final,
            1,
            expected_session=CLAUDE_SESSION_ID,
        )
        self.assertEqual(summary["usage_status"], "partial")
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        call_usage = usage["calls"][0]["usage"]
        self.assertNotIn("input_tokens", call_usage)
        self.assertNotIn("output_tokens", call_usage)
        self.assertEqual(usage["cost"], 0.1)
        self.assertIn("input_tokens", usage["usage_error"])
        self.assertIn("output_tokens", usage["usage_error"])


class DshTests(RuntimeTestBase):
    def test_initial_turn_extracts_session_and_final_without_usage(self):
        write_jsonl(self.events, dsh_events())

        summary = self.inspect_host(
            "dsh",
            self.events,
            self.final,
            1,
            expected_session=DSH_SESSION_ID,
        )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["session_id"], DSH_SESSION_ID)
        self.assertEqual(summary["usage_status"], "unavailable")
        self.assertEqual(summary["usage_call_count"], 0)
        self.assertEqual(summary["cost_status"], "unavailable")
        self.assertEqual(summary["cache_ratio_denominator"], "unavailable")
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["host"], "dsh")
        self.assertEqual(usage["turn_count"], 1)
        self.assertEqual(usage["turns"][0]["call_count"], 0)
        self.assertIsNone(usage["totals"])
        self.assertIsNone(usage["cost"])
        self.assertIsNone(usage["cost_scope"])
        self.assertIn("no documented", usage["usage_error"])
        self.assertEqual(
            self.final.read_text(encoding="utf-8"),
            "DSH_FINAL\nPREWALK_PHASE: NAVIGATOR_REVIEW",
        )

    def test_resumed_turn_is_idempotent_and_keeps_usage_unavailable(self):
        write_jsonl(self.events, dsh_events("TURN_ONE"))
        self.inspect_host(
            "dsh",
            self.events,
            self.final,
            1,
            expected_session=DSH_SESSION_ID,
        )

        write_jsonl(self.events2, dsh_events("TURN_TWO"))
        summary = self.inspect_host(
            "dsh",
            self.events2,
            self.final2,
            2,
            expected_session=DSH_SESSION_ID,
        )
        self.assertEqual(summary["usage_turn_count"], 2)
        self.assertEqual(summary["usage_call_count"], 0)

        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual([turn["call_count"] for turn in usage["turns"]], [0, 0])
        self.assertEqual(
            usage["turns"][1]["source_events"], [str(self.events2)]
        )
        before = self.usage.read_text(encoding="utf-8")
        self.inspect_host(
            "dsh",
            self.events2,
            self.final2,
            2,
            expected_session=DSH_SESSION_ID,
        )
        self.assertEqual(self.usage.read_text(encoding="utf-8"), before)

    def test_error_incomplete_or_missing_final_is_rejected(self):
        for index, reason in enumerate(("error", "failed", "interrupted")):
            with self.subTest(reason=reason):
                events = self.root / f"dsh-error-{index}.jsonl"
                final = self.root / f"dsh-error-{index}.txt"
                usage = self.root / f"dsh-error-{index}.json"
                write_jsonl(events, dsh_events(reason=reason))
                with self.assertRaises(RUNTIME.InspectError):
                    self.inspect_host(
                        "dsh",
                        events,
                        final,
                        1,
                        expected_session=DSH_SESSION_ID,
                    )
                self.assertFalse(final.exists())
                self.assertFalse(usage.exists())

        write_jsonl(self.events, dsh_events(reason=MISSING))
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_host("dsh", self.events, self.final, 1)

        write_jsonl(self.events, dsh_events(text=None))
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_host("dsh", self.events, self.final, 1)

        write_jsonl(
            self.events,
            [{"type": "session", "sessionId": DSH_SESSION_ID}],
        )
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_host("dsh", self.events, self.final, 1)
        self.assertFalse(self.final.exists())
        self.assertFalse(self.usage.exists())

    def test_conflicting_session_ids_are_rejected(self):
        write_jsonl(
            self.events,
            dsh_events(session_id="dsh-a", final_id="dsh-b"),
        )
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_host("dsh", self.events, self.final, 1)
        self.assertFalse(self.final.exists())

    def test_usage_like_events_are_not_inferred(self):
        write_jsonl(
            self.events,
            dsh_events(
                extra=[
                    {
                        "type": "usage",
                        "usage": {
                            "input_tokens": 999,
                            "output_tokens": 999,
                        },
                    },
                    {"type": "reasoning", "text": "hidden reasoning"},
                    {"type": "tool", "name": "shell"},
                ]
            ),
        )
        summary = self.inspect_host(
            "dsh",
            self.events,
            self.final,
            1,
            expected_session=DSH_SESSION_ID,
        )
        self.assertEqual(summary["usage_status"], "unavailable")
        self.assertEqual(summary["usage_call_count"], 0)
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["calls"], [])
        self.assertIsNone(usage["totals"])
        self.assertIsNone(usage["cost"])


class GenericAdapterTests(RuntimeTestBase):
    def test_initial_turn_aggregates_usage_calls_and_separate_cache_ratio(self):
        usages = [
            generic_usage(
                {
                    "input_other": 100,
                    "cache_read": 50,
                    "cache_creation": 25,
                    "output": 10,
                    "reasoning_output": 2,
                },
                cost=None,
                cost_status="unavailable",
                scope="turn",
                model="model-a",
            ),
            generic_usage(
                {
                    "input_other": 40,
                    "cache_read": 160,
                    "cache_creation": 0,
                    "output": 5,
                },
                cost=None,
                cost_status="unavailable",
            ),
        ]
        write_jsonl(self.events, generic_events(usages=usages))

        summary = self.inspect_host(
            "generic",
            self.events,
            self.final,
            1,
            expected_session=GENERIC_SESSION_ID,
        )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["usage_turn_count"], 1)
        self.assertEqual(summary["usage_call_count"], 2)
        self.assertEqual(summary["usage_status"], "available")
        self.assertEqual(summary["cost_status"], "unavailable")
        self.assertEqual(
            summary["cache_ratio_denominator"],
            "input_other+cache_read+cache_creation",
        )
        self.assertAlmostEqual(summary["cache_read_ratio"], 210 / 375)

        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["host"], "generic")
        self.assertEqual(usage["call_count"], 2)
        turn = usage["turns"][0]
        self.assertEqual(turn["input_other"], 140)
        self.assertEqual(turn["cache_read"], 210)
        self.assertEqual(turn["cache_creation"], 25)
        self.assertEqual(turn["output"], 15)
        self.assertEqual(turn["reasoning_output"], 2)
        self.assertEqual(turn["input_total"], 375)
        self.assertAlmostEqual(turn["cache_read_ratio"], 210 / 375)
        self.assertEqual(usage["totals"]["input_total"], 375)
        self.assertAlmostEqual(usage["calls"][0]["cache_read_ratio"], 50 / 175)
        self.assertAlmostEqual(usage["calls"][1]["cache_read_ratio"], 160 / 200)
        self.assertEqual(usage["models"], ["model-a"])
        self.assertEqual(
            [call["scope"] for call in usage["calls"]], ["turn", "turn"]
        )
        self.assertIsNone(usage["cost"])
        self.assertEqual(usage["cost_status"], "unavailable")

    def test_resumed_turn_preserves_prior_usage_and_is_idempotent(self):
        write_jsonl(
            self.events,
            generic_events(
                usages=[
                    generic_usage(
                        {"input_other": 10, "cache_read": 0, "output": 1},
                        cost_status="unavailable",
                    )
                ]
            ),
        )
        self.inspect_host(
            "generic",
            self.events,
            self.final,
            1,
            expected_session=GENERIC_SESSION_ID,
        )

        write_jsonl(
            self.events2,
            generic_events(
                usages=[
                    generic_usage(
                        {"input_other": 20, "cache_read": 10, "output": 2},
                        cost_status="unavailable",
                    ),
                    generic_usage(
                        {"input_other": 30, "cache_read": 20, "output": 3},
                        cost_status="unavailable",
                    ),
                ]
            ),
        )
        summary = self.inspect_host(
            "generic",
            self.events2,
            self.final2,
            2,
            expected_session=GENERIC_SESSION_ID,
        )
        self.assertEqual(summary["usage_turn_count"], 2)
        self.assertEqual(summary["usage_call_count"], 3)

        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual([turn["call_count"] for turn in usage["turns"]], [1, 2])
        self.assertEqual(usage["totals"]["input_other"], 60)
        self.assertEqual(usage["totals"]["cache_read"], 30)
        self.assertEqual(usage["totals"]["input_total"], 90)
        self.assertAlmostEqual(usage["totals"]["cache_read_ratio"], 30 / 90)
        self.assertEqual(
            [call["turn_call"] for call in usage["calls"] if call["builder_turn"] == 2],
            [1, 2],
        )

        before = self.usage.read_text(encoding="utf-8")
        repeat = self.inspect_host(
            "generic",
            self.events2,
            self.final2,
            2,
            expected_session=GENERIC_SESSION_ID,
        )
        self.assertEqual(repeat["usage_call_count"], 3)
        self.assertEqual(self.usage.read_text(encoding="utf-8"), before)

        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_host(
                "generic",
                self.events,
                self.final,
                1,
                expected_session=GENERIC_SESSION_ID,
            )
        self.assertEqual(self.usage.read_text(encoding="utf-8"), before)

    def test_session_cumulative_estimated_cost_is_not_summed(self):
        write_jsonl(
            self.events,
            generic_events(
                usages=[
                    generic_usage(
                        {"input_other": 10, "output": 1},
                        cost=0.10,
                        cost_status="estimated",
                        cost_scope="session_cumulative",
                    )
                ]
            ),
        )
        self.inspect_host(
            "generic",
            self.events,
            self.final,
            1,
            expected_session=GENERIC_SESSION_ID,
        )

        write_jsonl(
            self.events2,
            generic_events(
                usages=[
                    generic_usage(
                        {"input_other": 20, "output": 2},
                        cost=0.18,
                        cost_status="estimated",
                        cost_scope="session_cumulative",
                    )
                ]
            ),
        )
        summary = self.inspect_host(
            "generic",
            self.events2,
            self.final2,
            2,
            expected_session=GENERIC_SESSION_ID,
        )
        self.assertEqual(summary["cost_status"], "estimated")
        self.assertEqual(summary["cost_scope"], "session_cumulative")

        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual([call["cost"] for call in usage["calls"]], [0.10, 0.18])
        self.assertEqual(usage["cost"], 0.18)
        self.assertNotEqual(usage["cost"], 0.28)
        self.assertEqual(usage["cost_status"], "estimated")
        self.assertEqual(usage["cost_scope"], "session_cumulative")

    def test_call_scoped_reported_cost_is_preserved(self):
        usages = [
            generic_usage(
                {"input_other": 10, "output": 1},
                cost=0.01,
                cost_status="reported",
                cost_scope="call",
            ),
            generic_usage(
                {"input_other": 20, "output": 2},
                cost=0.02,
                cost_status="reported",
                cost_scope="call",
            ),
        ]
        write_jsonl(self.events, generic_events(usages=usages))
        self.inspect_host(
            "generic",
            self.events,
            self.final,
            1,
            expected_session=GENERIC_SESSION_ID,
        )

        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["cost_status"], "reported")
        self.assertEqual(usage["cost_scope"], "call")
        self.assertEqual(
            usage["cost"],
            [
                {"builder_turn": 1, "call": 1, "value": 0.01},
                {"builder_turn": 1, "call": 2, "value": 0.02},
            ],
        )

        single_path = self.root / "usage-single-reported.json"
        single_final = self.root / "final-single-reported.txt"
        write_jsonl(
            self.events,
            generic_events(usages=[usages[0]]),
        )
        self.inspect_host(
            "generic",
            self.events,
            single_final,
            1,
            expected_session=GENERIC_SESSION_ID,
            usage_path=single_path,
        )
        single = json.loads(single_path.read_text(encoding="utf-8"))
        self.assertEqual(single["cost"], 0.01)
        self.assertEqual(single["cost_status"], "reported")
        self.assertEqual(single["cost_scope"], "call")

    def test_included_total_cache_family(self):
        write_jsonl(
            self.events,
            generic_events(
                usages=[
                    generic_usage(
                        {
                            "generic_input_tokens": 1000,
                            "generic_cached_input_tokens": 800,
                            "output": 10,
                        },
                        cost_status="unavailable",
                    )
                ]
            ),
        )
        summary = self.inspect_host(
            "generic",
            self.events,
            self.final,
            1,
            expected_session=GENERIC_SESSION_ID,
        )
        self.assertEqual(
            summary["cache_ratio_denominator"], "generic_input_tokens"
        )
        self.assertAlmostEqual(summary["cache_read_ratio"], 0.8)
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["totals"]["generic_input_tokens"], 1000)
        self.assertEqual(usage["totals"]["generic_cached_input_tokens"], 800)
        self.assertEqual(usage["totals"]["input_total"], 1000)
        self.assertAlmostEqual(usage["totals"]["cache_read_ratio"], 0.8)
        self.assertNotIn("input_other", usage["totals"])

    def test_missing_usage_is_unavailable_not_zero(self):
        write_jsonl(self.events, generic_events(usages=[]))
        summary = self.inspect_host(
            "generic",
            self.events,
            self.final,
            1,
            expected_session=GENERIC_SESSION_ID,
        )
        self.assertEqual(summary["usage_status"], "unavailable")
        self.assertEqual(summary["usage_call_count"], 0)
        usage = json.loads(self.usage.read_text(encoding="utf-8"))
        self.assertEqual(usage["turn_count"], 1)
        self.assertEqual(usage["turns"][0]["call_count"], 0)
        self.assertIsNone(usage["totals"])
        self.assertEqual(usage["cost_status"], "unavailable")
        self.assertIn("no generic usage events", usage["usage_error"])
        self.assertEqual(
            self.final.read_text(encoding="utf-8"),
            "GENERIC_FINAL\nPREWALK_PHASE: NAVIGATOR_REVIEW",
        )

    def test_present_session_scope_is_rejected_without_overwrite(self):
        write_jsonl(
            self.events,
            generic_events(
                usages=[
                    generic_usage(
                        {"input_other": 10, "output": 1},
                        cost_status="unavailable",
                        scope="session",
                    )
                ]
            ),
        )
        self.final.write_text("keep-final", encoding="utf-8")
        prior = {
            "schema_version": 2,
            "host": "generic",
            "session_id": GENERIC_SESSION_ID,
            "turns": [],
            "calls": [],
            "skipped_scope_count": 0,
        }
        prior_text = json.dumps(prior, indent=2) + "\n"
        self.usage.write_text(prior_text, encoding="utf-8")

        with self.assertRaises(RUNTIME.InspectError) as caught:
            self.inspect_host(
                "generic",
                self.events,
                self.final,
                1,
                expected_session=GENERIC_SESSION_ID,
            )
        self.assertIn("scope", str(caught.exception))
        self.assertEqual(self.final.read_text(encoding="utf-8"), "keep-final")
        self.assertEqual(self.usage.read_text(encoding="utf-8"), prior_text)

    def test_malformed_generic_shapes_fail_safely(self):
        cases = {
            "completed_false": generic_events(completed=False),
            "completed_missing": generic_events(completed=MISSING),
            "missing_final_text": generic_events(final=None),
            "missing_session": generic_events(
                include_session=False, final_id=None
            ),
            "conflicting_sessions": generic_events(
                session_id="generic-a", final_id="generic-b"
            ),
            "missing_final": [{"type": "session", "session_id": GENERIC_SESSION_ID}],
            "usage_not_object": generic_events(
                usages=[{"type": "usage", "usage": "bad"}]
            ),
            "unknown_token_fields": generic_events(
                usages=[
                    generic_usage({"foo": 1}, cost_status="unavailable")
                ]
            ),
            "negative_token": generic_events(
                usages=[
                    generic_usage(
                        {"input_other": -1}, cost_status="unavailable"
                    )
                ]
            ),
            "string_token": generic_events(
                usages=[
                    generic_usage(
                        {"input_other": "1"}, cost_status="unavailable"
                    )
                ]
            ),
            "mixed_families": generic_events(
                usages=[
                    generic_usage(
                        {"input_other": 1, "generic_input_tokens": 2},
                        cost_status="unavailable",
                    )
                ]
            ),
            "invalid_cost_status": generic_events(
                usages=[
                    generic_usage(
                        {"input_other": 1},
                        cost=0.1,
                        cost_status="free",
                    )
                ]
            ),
            "invalid_cost_scope": generic_events(
                usages=[
                    generic_usage(
                        {"input_other": 1},
                        cost=0.1,
                        cost_status="reported",
                        cost_scope="turn",
                    )
                ]
            ),
            "reported_without_cost": generic_events(
                usages=[
                    generic_usage(
                        {"input_other": 1}, cost_status="reported"
                    )
                ]
            ),
            "unavailable_with_cost": generic_events(
                usages=[
                    generic_usage(
                        {"input_other": 1},
                        cost=0.1,
                        cost_status="unavailable",
                    )
                ]
            ),
            "included_without_total": generic_events(
                usages=[
                    generic_usage(
                        {"generic_cached_input_tokens": 5},
                        cost_status="unavailable",
                    )
                ]
            ),
            "invalid_session_id_type": [
                {"type": "session", "session_id": 5},
                {"type": "final", "completed": True, "text": "text"},
            ],
        }
        for name, events in cases.items():
            with self.subTest(name=name):
                events_path = self.root / f"generic-bad-{name}.jsonl"
                final_path = self.root / f"generic-bad-{name}.txt"
                usage_path = self.root / f"generic-bad-{name}.json"
                write_jsonl(events_path, events)
                with self.assertRaises(RUNTIME.InspectError):
                    self.inspect_host(
                        "generic",
                        events_path,
                        final_path,
                        1,
                        expected_session=GENERIC_SESSION_ID,
                        usage_path=usage_path,
                    )
                self.assertFalse(final_path.exists())
                self.assertFalse(usage_path.exists())

    def test_resumed_turn_requires_prior_usage_and_matching_session(self):
        write_jsonl(
            self.events2,
            generic_events(
                usages=[
                    generic_usage(
                        {"input_other": 10, "output": 1},
                        cost_status="unavailable",
                    )
                ]
            ),
        )
        self.final2.write_text("keep-final", encoding="utf-8")
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_host(
                "generic",
                self.events2,
                self.final2,
                2,
                expected_session=GENERIC_SESSION_ID,
            )
        self.assertEqual(self.final2.read_text(encoding="utf-8"), "keep-final")
        self.assertFalse(self.usage.exists())

        write_jsonl(
            self.events,
            generic_events(
                usages=[
                    generic_usage(
                        {"input_other": 10, "output": 1},
                        cost_status="unavailable",
                    )
                ]
            ),
        )
        self.inspect_host(
            "generic",
            self.events,
            self.final,
            1,
            expected_session=GENERIC_SESSION_ID,
        )
        before = self.usage.read_text(encoding="utf-8")
        with self.assertRaises(RUNTIME.InspectError):
            self.inspect_host(
                "generic",
                self.events2,
                self.final2,
                2,
                expected_session="generic-other",
            )
        self.assertEqual(self.usage.read_text(encoding="utf-8"), before)
        self.assertEqual(self.final2.read_text(encoding="utf-8"), "keep-final")


class NewHostCliTests(RuntimeTestBase):
    def test_cli_generic_host_reports_summary_without_final_text(self):
        write_jsonl(
            self.events,
            generic_events(
                usages=[
                    generic_usage(
                        {"input_other": 10, "output": 1},
                        cost_status="unavailable",
                    )
                ]
            ),
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = RUNTIME.main(
                [
                    "inspect",
                    "--host",
                    "generic",
                    "--builder-turn",
                    "1",
                    "--events",
                    str(self.events),
                    "--expected-session",
                    GENERIC_SESSION_ID,
                    "--final-message",
                    str(self.final),
                    "--usage",
                    str(self.usage),
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["host"], "generic")
        self.assertEqual(payload["session_id"], GENERIC_SESSION_ID)
        self.assertEqual(payload["usage_call_count"], 1)
        self.assertEqual(payload["cost_status"], "unavailable")
        self.assertNotIn("GENERIC_FINAL", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()


class ReturnPackageTests(RuntimeTestBase):
    def _inspect(self, final: str, handoff_id: str | None):
        write_jsonl(self.events, generic_events(final=final))
        return RUNTIME.inspect(
            host="generic",
            events_path=self.events,
            final_message_path=self.final,
            usage_path=self.usage,
            builder_turn=1,
            handoff_id=handoff_id,
        )

    def test_without_flag_legacy_behaviour_is_unchanged(self):
        self.assertTrue(self._inspect("no package", None)["ok"])

    def test_matching_package_is_accepted(self):
        final = "done\nPREWALK_PHASE: NAVIGATOR_REVIEW\nHANDOFF_ID: h-1\n"
        self.assertTrue(self._inspect(final, "h-1")["ok"])

    def test_legacy_marker_is_accepted(self):
        final = "PREWALK_PHASE: SOL_REVIEW\nHANDOFF_ID: h-1"
        self.assertTrue(self._inspect(final, "h-1")["ok"])

    def test_missing_marker_is_rejected_without_writing_usage(self):
        with self.assertRaises(RUNTIME.InspectError):
            self._inspect("HANDOFF_ID: h-1", "h-1")
        self.assertFalse(self.usage.exists())

    def test_wrong_handoff_id_is_rejected(self):
        with self.assertRaises(RUNTIME.InspectError):
            self._inspect("PREWALK_PHASE: NAVIGATOR_REVIEW\nHANDOFF_ID: other", "h-1")
