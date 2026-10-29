#!/usr/bin/env python3
"""Inspect Prewalk Builder turn artifacts and summarize host-reported usage.

The adapter reads one Builder turn's JSONL event stream, extracts the Builder
session id and final assistant message, and writes a cumulative usage record.
A Builder turn is one process invocation; a model call is one provider usage
record. The two are reported separately and never conflated.

Usage and cost are reported only when the host actually exposes them. The
adapter never prints prompts, conversations, or full model output.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 2

KIMI_TOKEN_KEYS = ("inputOther", "inputCacheRead", "inputCacheCreation", "output")
KIMI_INPUT_KEYS = ("inputOther", "inputCacheRead", "inputCacheCreation")
CODEX_TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
CLAUDE_INPUT_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
CLAUDE_TOKEN_KEYS = (*CLAUDE_INPUT_KEYS, "output_tokens")
GENERIC_FAMILY_A_INPUT_KEYS = ("input_other", "cache_read", "cache_creation")
GENERIC_FAMILY_B_INPUT_KEYS = ("generic_input_tokens", "generic_cached_input_tokens")
GENERIC_TOKEN_KEYS = (
    *GENERIC_FAMILY_A_INPUT_KEYS,
    *GENERIC_FAMILY_B_INPUT_KEYS,
    "output",
    "reasoning_output",
)
COST_STATUSES = ("reported", "estimated", "unavailable")
COST_SCOPES = ("call", "session_cumulative")
COST_KEYS = ("cost", "total_cost", "cost_usd", "total_cost_usd")


class InspectError(Exception):
    """A safe, user-facing inspection failure."""


@dataclass
class PriorUsage:
    calls: list[dict[str, Any]]
    turns: list[dict[str, Any]]
    skipped_scope_count: int = 0
    current_turn_hint: int | None = None


def _as_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    return None


def _ratio(numerator: Any, denominator: Any) -> float | None:
    num = _as_number(numerator)
    den = _as_number(denominator)
    if num is None or den is None or den <= 0:
        return None
    return num / den


def _iso_from_millis(value: Any) -> str | None:
    millis = _as_number(value)
    if millis is None:
        return None
    try:
        stamp = _datetime.datetime.fromtimestamp(
            millis / 1000.0, tz=_datetime.timezone.utc
        )
    except (OverflowError, OSError, ValueError):
        return None
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _read_jsonl(path: Path) -> list[tuple[int, dict[str, Any]]]:
    events: list[tuple[int, dict[str, Any]]] = []
    try:
        handle = Path(path).open("r", encoding="utf-8-sig")
    except OSError as exc:
        raise InspectError(f"cannot read events file: {exc}") from None
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise InspectError(
                    f"malformed JSONL at line {line_number}: {exc.msg}"
                ) from None
            if not isinstance(event, dict):
                raise InspectError(
                    f"malformed JSONL at line {line_number}: expected a JSON object"
                )
            events.append((line_number, event))
    if not events:
        raise InspectError("events file contains no JSONL records")
    return events


def _single_session_id(candidates: Iterable[str], host: str) -> str:
    ids = {value for value in candidates if value}
    if not ids:
        raise InspectError(f"{host} events contain no Builder session id")
    if len(ids) > 1:
        raise InspectError(f"{host} events contain conflicting Builder session ids")
    return ids.pop()


def _assistant_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") in (None, "text") and isinstance(
                    block.get("text"), str
                ):
                    parts.append(block["text"])
        if parts:
            return "".join(parts)
    return None


def _kimi_session_id(events: list[tuple[int, dict[str, Any]]]) -> str:
    return _single_session_id(
        (
            event.get("session_id")
            for _, event in events
            if event.get("type") == "session.resume_hint"
            and isinstance(event.get("session_id"), str)
        ),
        "Kimi",
    )


def _kimi_final_message(events: list[tuple[int, dict[str, Any]]]) -> str:
    final: str | None = None
    for _, event in events:
        if event.get("role") != "assistant":
            continue
        text = _assistant_text(event.get("content"))
        if text is not None and text.strip():
            final = text
    if final is None:
        raise InspectError("Kimi events contain no final assistant message")
    return final


def _codex_session_id(events: list[tuple[int, dict[str, Any]]]) -> str:
    return _single_session_id(
        (
            event.get("thread_id")
            for _, event in events
            if event.get("type") == "thread.started"
            and isinstance(event.get("thread_id"), str)
        ),
        "Codex",
    )


def _codex_final_message(events: list[tuple[int, dict[str, Any]]]) -> str:
    final: str | None = None
    for _, event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            final = text
    if final is None:
        raise InspectError("Codex events contain no final agent message")
    return final


def _valid_cost(value: Any) -> Any:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    if isinstance(value, dict):
        amount = _as_number(value.get("amount"))
        if amount is None:
            return None
        currency = value.get("currency")
        if currency is not None and not isinstance(currency, str):
            return None
        result: dict[str, Any] = {"amount": amount}
        if isinstance(currency, str) and currency.strip():
            result["currency"] = currency.strip()
        return result
    return None


def _extract_cost(container: dict[str, Any]) -> Any:
    for key in COST_KEYS:
        if key in container:
            value = _valid_cost(container[key])
            if value is not None:
                return value
    usage = container.get("usage")
    if isinstance(usage, dict):
        for key in COST_KEYS:
            if key in usage:
                value = _valid_cost(usage[key])
                if value is not None:
                    return value
    return None


def _normalize_call_cost(call: dict[str, Any]) -> None:
    """Fill cost_status/cost_scope for legacy calls without inventing a cost."""
    value = _valid_cost(call.get("cost"))
    if value is None:
        value = _extract_cost(call)
    if value is None:
        call["cost"] = None
        call["cost_status"] = "unavailable"
        call["cost_scope"] = None
        return
    status = call.get("cost_status")
    if status not in COST_STATUSES or status == "unavailable":
        status = "reported"
    scope = call.get("cost_scope")
    if scope not in COST_SCOPES:
        scope = "call"
    call["cost"] = value
    call["cost_status"] = status
    call["cost_scope"] = scope


def _summarize_cost(calls: list[dict[str, Any]]) -> tuple[Any, str, str | None]:
    """Pick the latest cumulative estimate or list per-call values.

    A session-cumulative cost already covers earlier calls in the same session,
    so resumed turns retain the latest value instead of summing it.
    """
    entries = [
        call
        for call in calls
        if call.get("cost") is not None
        and call.get("cost_status") in ("reported", "estimated")
    ]
    cumulative = [
        call
        for call in entries
        if call.get("cost_scope") == "session_cumulative"
    ]
    if cumulative:
        latest = max(
            cumulative, key=lambda call: (call["builder_turn"], call["call"])
        )
        return latest["cost"], latest["cost_status"], "session_cumulative"

    call_scoped = [call for call in entries if call.get("cost_scope") == "call"]
    if not call_scoped:
        return None, "unavailable", None
    if len(call_scoped) == 1:
        return call_scoped[0]["cost"], call_scoped[0]["cost_status"], "call"
    values = [
        {
            "builder_turn": call["builder_turn"],
            "call": call["call"],
            "value": call["cost"],
        }
        for call in call_scoped
    ]
    status = (
        "reported"
        if all(call["cost_status"] == "reported" for call in call_scoped)
        else "estimated"
    )
    return values, status, "call"


def _sum_known(
    records: list[dict[str, Any]], keys: Iterable[str]
) -> dict[str, int | float]:
    totals: dict[str, int | float] = {}
    for key in keys:
        values = [
            number
            for record in records
            if (number := _as_number(record.get(key))) is not None
        ]
        if values:
            totals[key] = sum(values)
    return totals


def _token_keys_for(host: str) -> tuple[str, ...]:
    if host == "kimi":
        return KIMI_TOKEN_KEYS
    if host == "codex":
        return CODEX_TOKEN_KEYS
    if host == "claude-code":
        return CLAUDE_TOKEN_KEYS
    if host == "generic":
        return GENERIC_TOKEN_KEYS
    return ()


def _generic_family(usage: dict[str, Any]) -> str | None:
    separate = any(key in usage for key in GENERIC_FAMILY_A_INPUT_KEYS)
    included = any(key in usage for key in GENERIC_FAMILY_B_INPUT_KEYS)
    if separate and included:
        raise InspectError(
            "generic usage mixes separate-cache and included-total token families"
        )
    if included:
        return "included"
    if separate:
        return "separate"
    return None


def _add_input_totals(host: str, totals: dict[str, Any]) -> None:
    if host == "kimi":
        if any(key in totals for key in KIMI_INPUT_KEYS):
            input_total = sum(totals.get(key, 0) for key in KIMI_INPUT_KEYS)
            totals["input_total"] = input_total
            totals["cache_read_ratio"] = _ratio(
                totals.get("inputCacheRead"), input_total
            )
    elif host == "claude-code":
        if any(key in totals for key in CLAUDE_INPUT_KEYS):
            input_total = sum(totals.get(key, 0) for key in CLAUDE_INPUT_KEYS)
            totals["input_total"] = input_total
            totals["cache_read_ratio"] = _ratio(
                totals.get("cache_read_input_tokens"), input_total
            )
    elif host == "generic":
        separate = any(key in totals for key in GENERIC_FAMILY_A_INPUT_KEYS)
        included = any(key in totals for key in GENERIC_FAMILY_B_INPUT_KEYS)
        if separate and included:
            raise InspectError(
                "generic usage mixes separate-cache and included-total token "
                "families across calls"
            )
        if included:
            totals["input_total"] = totals.get("generic_input_tokens")
            totals["cache_read_ratio"] = _ratio(
                totals.get("generic_cached_input_tokens"),
                totals.get("generic_input_tokens"),
            )
        elif separate:
            input_total = sum(
                totals.get(key, 0) for key in GENERIC_FAMILY_A_INPUT_KEYS
            )
            totals["input_total"] = input_total
            totals["cache_read_ratio"] = _ratio(
                totals.get("cache_read"), input_total
            )
    elif host == "codex" and "input_tokens" in totals:
        totals["input_total"] = totals["input_tokens"]
        totals["cache_read_ratio"] = _ratio(
            totals.get("cached_input_tokens"),
            totals.get("input_tokens"),
        )


def _aggregate_turn(
    host: str,
    turn: int,
    calls: list[dict[str, Any]],
    source_events: list[str],
) -> dict[str, Any]:
    aggregate: dict[str, Any] = {
        "turn": turn,
        "call_count": len(calls),
        "source_events": list(source_events),
    }
    usage_fields = [
        call["usage"] for call in calls if isinstance(call.get("usage"), dict)
    ]
    totals = _sum_known(usage_fields, _token_keys_for(host))
    _add_input_totals(host, totals)
    aggregate.update(totals)
    return aggregate


def _read_prior_file(path: Path) -> dict[str, Any] | None:
    path = Path(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise InspectError("prior usage path exists but is not a file")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise InspectError(f"cannot read prior usage file: {exc}") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InspectError(
            f"prior usage file is not valid JSON: {exc.msg}"
        ) from None
    if not isinstance(data, dict):
        raise InspectError("prior usage file has an unexpected shape")
    return data


def _validate_prior_turn(turn: Any) -> dict[str, Any]:
    if not isinstance(turn, dict):
        raise InspectError("prior usage turns must be objects")
    number = turn.get("turn")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise InspectError("prior usage turn numbers must be positive integers")
    call_count = turn.get("call_count")
    if (
        not isinstance(call_count, int)
        or isinstance(call_count, bool)
        or call_count < 0
    ):
        raise InspectError("prior usage call_count must be a non-negative integer")
    source_events = turn.get("source_events", [])
    if not isinstance(source_events, list) or any(
        not isinstance(item, str) for item in source_events
    ):
        raise InspectError("prior usage source_events must be a list of strings")
    return {
        "turn": number,
        "call_count": call_count,
        "source_events": list(source_events),
    }


def _load_prior_usage(
    usage_path: Path,
    host: str,
    session_id: str,
    builder_turn: int,
) -> PriorUsage | None:
    data = _read_prior_file(usage_path)
    if data is None:
        if builder_turn > 1:
            raise InspectError(
                f"--builder-turn {builder_turn} requires an existing cumulative "
                "usage file from the previous turn"
            )
        return None

    schema = data.get("schema_version")
    if schema == 1:
        if builder_turn > 1:
            raise InspectError(
                "schema_version 1 prior usage is not compatible with a resumed "
                "Builder turn; re-inspect builder turn 1 to create schema_version 2"
            )
        prior_host = data.get("host")
        prior_session = data.get("session_id")
        if prior_host is not None and prior_host != host:
            raise InspectError("prior usage host does not match the current host")
        if prior_session is not None and prior_session != session_id:
            raise InspectError(
                "prior usage session does not match the current Builder session"
            )
        turns = data.get("turns")
        if not isinstance(turns, list):
            raise InspectError("schema_version 1 prior usage has no turns list")
        hint = len(turns) if turns else None
        return PriorUsage([], [], 0, hint)

    if schema != 2:
        raise InspectError(
            f"unsupported prior usage schema_version: {schema!r}"
        )
    if data.get("host") != host:
        raise InspectError("prior usage host does not match the current host")
    if data.get("session_id") != session_id:
        raise InspectError(
            "prior usage session does not match the current Builder session"
        )
    raw_turns = data.get("turns")
    raw_calls = data.get("calls")
    if not isinstance(raw_turns, list) or not isinstance(raw_calls, list):
        raise InspectError("prior usage schema_version 2 requires turns and calls lists")

    turns = sorted(
        (_validate_prior_turn(turn) for turn in raw_turns),
        key=lambda item: item["turn"],
    )
    turn_numbers = [turn["turn"] for turn in turns]
    if turn_numbers != list(range(1, len(turns) + 1)):
        raise InspectError(
            "prior usage turns must be contiguous starting at builder turn 1"
        )

    turn_count = len(turns)
    if builder_turn > 1:
        if turn_count not in (builder_turn - 1, builder_turn):
            raise InspectError(
                f"prior usage has turns {turn_numbers}; builder turn "
                f"{builder_turn} needs turns 1..{builder_turn - 1} or 1..{builder_turn}"
            )
    elif turn_count not in (0, 1):
        raise InspectError(
            "cannot rewrite builder turn 1 when a later turn is already recorded"
        )

    grouped: dict[int, list[dict[str, Any]]] = {
        number: [] for number in turn_numbers
    }
    normalized_calls: list[dict[str, Any]] = []
    for call in raw_calls:
        if not isinstance(call, dict):
            raise InspectError("prior usage calls must be objects")
        call_turn = call.get("builder_turn")
        if (
            not isinstance(call_turn, int)
            or isinstance(call_turn, bool)
            or call_turn not in grouped
        ):
            raise InspectError(
                "prior usage calls must reference an existing builder turn"
            )
        if "usage" not in call:
            raise InspectError("prior usage calls must include raw usage")
        status = call.get("cost_status")
        scope = call.get("cost_scope")
        if status is not None and status not in COST_STATUSES:
            raise InspectError(
                "prior usage calls contain an invalid cost_status"
            )
        if scope is not None and scope not in COST_SCOPES:
            raise InspectError("prior usage calls contain an invalid cost_scope")
        if status in ("reported", "estimated") and _valid_cost(
            call.get("cost")
        ) is None:
            raise InspectError(
                "prior usage calls declare a cost status without a valid cost"
            )
        if status == "unavailable" and call.get("cost") is not None:
            raise InspectError(
                "prior usage calls declare unavailable cost with a cost value"
            )
        grouped[call_turn].append(call)
    for turn in turns:
        if len(grouped[turn["turn"]]) != turn["call_count"]:
            raise InspectError(
                "prior usage call counts do not match the recorded turns"
            )
    for number in turn_numbers:
        normalized_calls.extend(grouped[number])

    skipped = data.get("skipped_scope_count", 0)
    if (
        not isinstance(skipped, int)
        or isinstance(skipped, bool)
        or skipped < 0
    ):
        skipped = 0
    current_turn = next(
        (turn for turn in turns if turn["turn"] == builder_turn), None
    )
    return PriorUsage(
        normalized_calls,
        turns,
        skipped,
        current_turn["call_count"] if current_turn else None,
    )


def _finalize_usage(
    host: str,
    session_id: str,
    builder_turn: int,
    prior: PriorUsage | None,
    current_calls: list[dict[str, Any]],
    current_source_events: list[str],
    current_missing: bool,
    skipped_scope_count: int,
    source: dict[str, Any] | None,
    extra_errors: list[str] | None = None,
    missing_message: str | None = None,
) -> dict[str, Any]:
    prior_turns = [
        turn for turn in (prior.turns if prior else []) if turn["turn"] < builder_turn
    ]
    prior_calls = [
        call
        for call in (prior.calls if prior else [])
        if call["builder_turn"] < builder_turn
    ]
    call_groups: dict[int, list[dict[str, Any]]] = {}
    for call in prior_calls:
        call_groups.setdefault(call["builder_turn"], []).append(call)

    turns_out: list[dict[str, Any]] = []
    for turn in prior_turns:
        turns_out.append(
            _aggregate_turn(
                host,
                turn["turn"],
                call_groups.get(turn["turn"], []),
                turn.get("source_events", []),
            )
        )
    turns_out.append(
        _aggregate_turn(
            host,
            builder_turn,
            current_calls,
            current_source_events,
        )
    )

    calls_out: list[dict[str, Any]] = []
    for call in [*prior_calls, *current_calls]:
        copied = dict(call)
        copied["builder_turn"] = int(copied["builder_turn"])
        calls_out.append(copied)
    turn_call_numbers: dict[int, int] = {}
    for index, call in enumerate(calls_out, 1):
        call["call"] = index
        number = call["builder_turn"]
        turn_call_numbers[number] = turn_call_numbers.get(number, 0) + 1
        call["turn_call"] = turn_call_numbers[number]

    usage_fields = [
        call["usage"] for call in calls_out if isinstance(call.get("usage"), dict)
    ]
    totals = _sum_known(usage_fields, _token_keys_for(host))
    _add_input_totals(host, totals)
    totals_out: dict[str, Any] | None = totals or None

    models: list[str] = []
    for call in calls_out:
        model = call.get("model")
        if isinstance(model, str) and model and model not in models:
            models.append(model)

    total_skipped = max(
        prior.skipped_scope_count if prior else 0,
        skipped_scope_count,
    )
    errors: list[str] = list(extra_errors or [])
    if current_missing:
        message = missing_message or f"no usage records for builder turn {builder_turn}"
        if message not in errors:
            errors.append(message)
    if total_skipped > 0:
        message = f"excluded {total_skipped} non-turn usageScope records"
        if message not in errors:
            errors.append(message)

    if calls_out:
        usage_status = "partial" if errors else "available"
    elif total_skipped > 0:
        usage_status = "partial"
    else:
        usage_status = "unavailable"

    for call in calls_out:
        _normalize_call_cost(call)
    cost_value, cost_status, cost_scope = _summarize_cost(calls_out)

    if host == "codex":
        all_source_events = sorted(
            {
                event
                for turn in turns_out
                for event in turn.get("source_events", [])
            }
        )
        source_out: dict[str, Any] = {"events": all_source_events}
    else:
        source_out = source or {}

    return {
        "schema_version": SCHEMA_VERSION,
        "host": host,
        "session_id": session_id,
        "builder_turn": builder_turn,
        "usage_status": usage_status,
        "usage_error": "; ".join(errors) if errors else None,
        "calls": calls_out,
        "turns": turns_out,
        "totals": totals_out,
        "models": models,
        "call_count": len(calls_out),
        "turn_count": len(turns_out),
        "skipped_scope_count": total_skipped,
        "cache_ratio_denominator": _cache_ratio_denominator(host, totals_out),
        "cost": cost_value,
        "cost_status": cost_status,
        "cost_scope": cost_scope,
        "source": source_out,
    }


def _cache_ratio_denominator(
    host: str, totals: dict[str, Any] | None
) -> str:
    if host == "kimi":
        return "inputOther+inputCacheRead+inputCacheCreation"
    if host == "codex":
        return "input_tokens"
    if host == "claude-code":
        return (
            "input_tokens+cache_read_input_tokens+"
            "cache_creation_input_tokens"
        )
    if host == "dsh":
        return "unavailable"
    if totals and any(key in totals for key in GENERIC_FAMILY_B_INPUT_KEYS):
        return "generic_input_tokens"
    if totals and any(key in totals for key in GENERIC_FAMILY_A_INPUT_KEYS):
        return "input_other+cache_read+cache_creation"
    return "unavailable"


def _find_kimi_binary() -> str | None:
    override = os.environ.get("KIMI_BIN")
    candidates = [override] if override else []
    candidates.append(shutil.which("kimi"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return None


def _kimi_argv(kimi_bin: str, args: list[str]) -> list[str]:
    if kimi_bin.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", kimi_bin, *args]
    return [kimi_bin, *args]


def _read_kimi_wire(wire: Path) -> tuple[list[list[dict[str, Any]]], int]:
    segments: list[list[dict[str, Any]]] = []
    current_segment = -1
    boundary_count = 0
    try:
        handle = wire.open("r", encoding="utf-8-sig")
    except OSError as exc:
        raise InspectError(f"cannot read Kimi session usage: {exc}") from None
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise InspectError(
                    f"malformed Kimi session usage at line {line_number}: {exc.msg}"
                ) from None
            if not isinstance(record, dict):
                continue
            if record.get("type") == "turn.prompt":
                segments.append([])
                current_segment = len(segments) - 1
                boundary_count += 1
            elif record.get("type") == "usage.record":
                if current_segment < 0:
                    segments.append([])
                    current_segment = 0
                segments[current_segment].append(record)
    return segments, boundary_count


def _kimi_call(record: dict[str, Any], builder_turn: int, turn_call: int) -> dict[str, Any]:
    raw_usage = record.get("usage")
    usage_fields = raw_usage if isinstance(raw_usage, dict) else {}
    model = record.get("model")
    return {
        "builder_turn": builder_turn,
        "turn_call": turn_call,
        "model": model if isinstance(model, str) else None,
        "time": record.get("time"),
        "time_iso": _iso_from_millis(record.get("time")),
        "scope": record.get("usageScope"),
        "usage": raw_usage,
        "cache_read_ratio": _ratio(
            usage_fields.get("inputCacheRead"),
            sum(
                value
                for key in KIMI_INPUT_KEYS
                if (value := _as_number(usage_fields.get(key))) is not None
            ),
        ),
        "cost": _extract_cost(record),
    }


def _kimi_unavailable(
    session_id: str,
    builder_turn: int,
    prior: PriorUsage | None,
    events_path: Path,
    error: str,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _finalize_usage(
        "kimi",
        session_id,
        builder_turn,
        prior,
        [],
        [str(events_path)],
        False,
        0,
        source,
        extra_errors=[error],
    )


def _kimi_usage(
    session_id: str,
    cwd: Path,
    builder_turn: int,
    prior: PriorUsage | None,
    events_path: Path,
) -> dict[str, Any]:
    kimi_bin = _find_kimi_binary()
    if not kimi_bin:
        return _kimi_unavailable(
            session_id,
            builder_turn,
            prior,
            events_path,
            "kimi CLI not found; set KIMI_BIN",
        )
    command = _kimi_argv(
        kimi_bin,
        ["session", "list", "--cwd", str(cwd), "--json"],
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _kimi_unavailable(
            session_id,
            builder_turn,
            prior,
            events_path,
            f"cannot run kimi session list: {exc}",
        )
    if completed.returncode != 0:
        return _kimi_unavailable(
            session_id,
            builder_turn,
            prior,
            events_path,
            f"kimi session list exited {completed.returncode}",
        )
    try:
        sessions = json.loads(completed.stdout.lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        return _kimi_unavailable(
            session_id,
            builder_turn,
            prior,
            events_path,
            f"kimi session list returned invalid JSON: {exc.msg}",
        )
    if isinstance(sessions, dict):
        sessions = sessions.get("sessions", [])
    if not isinstance(sessions, list):
        return _kimi_unavailable(
            session_id,
            builder_turn,
            prior,
            events_path,
            "kimi session list returned an unexpected shape",
        )
    match = next(
        (
            item
            for item in sessions
            if isinstance(item, dict) and item.get("id") == session_id
        ),
        None,
    )
    if match is None:
        return _kimi_unavailable(
            session_id,
            builder_turn,
            prior,
            events_path,
            "Builder session id not found in kimi session list",
        )
    session_dir = match.get("sessionDir")
    if not isinstance(session_dir, str) or not session_dir:
        return _kimi_unavailable(
            session_id,
            builder_turn,
            prior,
            events_path,
            "matching Kimi session has no sessionDir",
        )
    wire = Path(session_dir) / "agents" / "main" / "wire.jsonl"
    source = {"session_dir": session_dir, "wire": str(wire)}
    if not wire.is_file():
        return _kimi_unavailable(
            session_id,
            builder_turn,
            prior,
            events_path,
            "Kimi session wire.jsonl is missing",
            source,
        )

    try:
        segments, boundary_count = _read_kimi_wire(wire)
    except InspectError as exc:
        return _kimi_unavailable(
            session_id,
            builder_turn,
            prior,
            events_path,
            str(exc),
            source,
        )
    all_records = [record for segment in segments for record in segment]
    if not all_records:
        return _finalize_usage(
            "kimi",
            session_id,
            builder_turn,
            prior,
            [],
            [str(events_path)],
            True,
            0,
            source,
            missing_message=f"no usage.record entries for builder turn {builder_turn}",
        )

    accounted_segments = [
        [
            record
            for record in segment
            if record.get("usageScope") in (None, "turn")
        ]
        for segment in segments
    ]
    accounted = [record for segment in accounted_segments for record in segment]
    skipped = len(all_records) - len(accounted)
    earlier_call_count = sum(
        turn["call_count"]
        for turn in (prior.turns if prior else [])
        if turn["turn"] < builder_turn
    )
    if prior is None and builder_turn == 1 and boundary_count > 0:
        current_records = accounted_segments[0] if accounted_segments else []
    else:
        if len(accounted) < earlier_call_count:
            raise InspectError(
                "prior usage expects "
                f"{earlier_call_count} earlier model calls but the Kimi session "
                f"wire has only {len(accounted)} accounted calls; refusing to "
                "rewrite the cumulative usage file"
            )
        hint = prior.current_turn_hint if prior else None
        if hint is not None:
            required = earlier_call_count + hint
            if len(accounted) < required:
                raise InspectError(
                    "prior usage expects "
                    f"{required} accounted model calls but the Kimi session "
                    f"wire has {len(accounted)}; refusing to rewrite the "
                    "cumulative usage file"
                )
            current_records = accounted[earlier_call_count:required]
        else:
            current_records = accounted[earlier_call_count:]

    current_calls = [
        _kimi_call(record, builder_turn, index)
        for index, record in enumerate(current_records, 1)
    ]
    current_missing = not current_calls
    return _finalize_usage(
        "kimi",
        session_id,
        builder_turn,
        prior,
        current_calls,
        [str(events_path)],
        current_missing,
        skipped,
        source,
        missing_message=f"no usage.record entries for builder turn {builder_turn}",
    )


def _codex_usage(
    session_id: str,
    events: list[tuple[int, dict[str, Any]]],
    builder_turn: int,
    prior: PriorUsage | None,
    events_path: Path,
) -> dict[str, Any]:
    records = [
        event
        for _, event in events
        if event.get("type") == "turn.completed"
        and isinstance(event.get("usage"), dict)
    ]
    current_calls: list[dict[str, Any]] = []
    for index, record in enumerate(records, 1):
        raw_usage = record["usage"]
        model = record.get("model")
        current_calls.append(
            {
                "builder_turn": builder_turn,
                "turn_call": index,
                "model": model if isinstance(model, str) else None,
                "time": record.get("time"),
                "time_iso": _iso_from_millis(record.get("time")),
                "scope": record.get("usage_scope", "turn"),
                "usage": raw_usage,
                "cache_read_ratio": _ratio(
                    raw_usage.get("cached_input_tokens"),
                    raw_usage.get("input_tokens"),
                ),
                "cost": _extract_cost(record),
            }
        )
    current_missing = not current_calls
    return _finalize_usage(
        "codex",
        session_id,
        builder_turn,
        prior,
        current_calls,
        [str(events_path)],
        current_missing,
        0,
        None,
        missing_message=f"no turn.completed usage for builder turn {builder_turn}",
    )


def _claude_result(
    events: list[tuple[int, dict[str, Any]]],
) -> tuple[str, str, dict[str, Any]]:
    results = [
        event for _, event in events if event.get("type") == "result"
    ]
    if not results:
        raise InspectError("Claude Code events contain no terminal result event")
    result = results[-1]

    candidates: list[str] = []
    for _, event in events:
        if event.get("type") == "result":
            session_id = event.get("session_id")
            if session_id is not None and not isinstance(session_id, str):
                raise InspectError(
                    "Claude Code result session_id must be a string"
                )
            if isinstance(session_id, str) and session_id:
                candidates.append(session_id)
        elif event.get("type") == "system" and event.get("subtype") == "init":
            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id:
                candidates.append(session_id)
    session_id = _single_session_id(candidates, "Claude Code")

    is_error = result.get("is_error")
    if is_error is not False:
        detail = result.get("result")
        if not isinstance(detail, str) or not detail.strip():
            detail = "no result text"
        raise InspectError(
            "Claude Code result is an error "
            f"(is_error={is_error!r}): {detail}"
        )
    text = result.get("result")
    if not isinstance(text, str) or not text.strip():
        raise InspectError("Claude Code result contains no final response text")
    return session_id, text, result


def _claude_model(result: dict[str, Any]) -> str | None:
    model_usage = result.get("modelUsage")
    if isinstance(model_usage, dict):
        names = [name for name in model_usage if isinstance(name, str) and name]
        if len(names) == 1:
            return names[0]
    model = result.get("model")
    return model if isinstance(model, str) and model else None


def _claude_usage(
    session_id: str,
    result: dict[str, Any],
    builder_turn: int,
    prior: PriorUsage | None,
    events_path: Path,
) -> dict[str, Any]:
    raw_usage = result.get("usage")
    errors: list[str] = []
    usage: dict[str, int | float] = {}

    if raw_usage is None:
        pass
    elif not isinstance(raw_usage, dict):
        errors.append(
            "Claude Code result usage is not an object; token usage is unavailable"
        )
    else:
        for key in CLAUDE_TOKEN_KEYS:
            if key not in raw_usage:
                continue
            value = _as_number(raw_usage[key])
            if value is None or value < 0:
                errors.append(
                    f"Claude Code usage field {key} is invalid and was ignored"
                )
                continue
            usage[key] = value

    cost = None
    cost_status = "unavailable"
    cost_scope = None
    raw_cost = result.get("total_cost_usd")
    if raw_cost is not None:
        cost = _valid_cost(raw_cost)
        if cost is None:
            errors.append(
                "Claude Code total_cost_usd is invalid and was ignored"
            )
        else:
            cost_status = "estimated"
            cost_scope = "session_cumulative"

    token_usage_missing = not usage
    if token_usage_missing and cost is not None:
        errors.append(
            "Claude Code result has cost evidence but no token usage; "
            "token usage is unavailable"
        )

    current_calls: list[dict[str, Any]] = []
    if usage or cost is not None:
        current_calls.append(
            {
                "builder_turn": builder_turn,
                "turn_call": 1,
                "model": _claude_model(result),
                "time": None,
                "time_iso": None,
                "scope": "turn",
                "usage": usage,
                "cache_read_ratio": _ratio(
                    usage.get("cache_read_input_tokens"),
                    sum(usage.get(key, 0) for key in CLAUDE_INPUT_KEYS),
                ),
                "cost": cost,
                "cost_status": cost_status,
                "cost_scope": cost_scope,
            }
        )

    return _finalize_usage(
        "claude-code",
        session_id,
        builder_turn,
        prior,
        current_calls,
        [str(events_path)],
        not current_calls,
        0,
        None,
        extra_errors=errors,
        missing_message=f"no Claude Code usage for builder turn {builder_turn}",
    )


def _dsh_final_text(final: dict[str, Any]) -> str:
    for key in ("text", "answer", "result", "content"):
        value = final.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            inner = value.get("text")
            if isinstance(inner, str) and inner.strip():
                return inner
            inner = _assistant_text(value.get("content"))
            if inner is not None and inner.strip():
                return inner
        if isinstance(value, list):
            inner = _assistant_text(value)
            if inner is not None and inner.strip():
                return inner
    message = final.get("message")
    if isinstance(message, str) and message.strip():
        return message
    if isinstance(message, dict):
        inner = message.get("text")
        if isinstance(inner, str) and inner.strip():
            return inner
        inner = _assistant_text(message.get("content"))
        if inner is not None and inner.strip():
            return inner
    if isinstance(message, list):
        inner = _assistant_text(message)
        if inner is not None and inner.strip():
            return inner
    raise InspectError("DSH final event contains no final assistant text")


def _dsh_result(
    events: list[tuple[int, dict[str, Any]]],
) -> tuple[str, str]:
    finals = [event for _, event in events if event.get("type") == "final"]
    if not finals:
        raise InspectError("DSH events contain no terminal final event")
    final = finals[-1]

    candidates: list[str] = []
    for _, event in events:
        if event.get("type") not in ("session", "final"):
            continue
        if "sessionId" not in event:
            if event.get("type") == "session":
                raise InspectError("DSH session event has no sessionId")
            continue
        session_id = event.get("sessionId")
        if not isinstance(session_id, str) or not session_id.strip():
            raise InspectError("DSH sessionId must be a non-empty string")
        candidates.append(session_id)
    session_id = _single_session_id(candidates, "DSH")

    reason = final.get("reason")
    if not isinstance(reason, dict):
        raise InspectError("DSH final event has no durable reason object")
    kind = reason.get("kind")
    if kind != "completed":
        raise InspectError(
            f"DSH final reason.kind is {kind!r}, not 'completed'"
        )
    return session_id, _dsh_final_text(final)


def _dsh_usage(
    session_id: str,
    builder_turn: int,
    prior: PriorUsage | None,
    events_path: Path,
) -> dict[str, Any]:
    return _finalize_usage(
        "dsh",
        session_id,
        builder_turn,
        prior,
        [],
        [str(events_path)],
        False,
        0,
        None,
        extra_errors=[
            "DSH exposes no documented token or cost payload; usage and cost "
            "are unavailable"
        ],
    )


def _generic_session_and_final(
    events: list[tuple[int, dict[str, Any]]],
) -> tuple[str, str]:
    finals = [event for _, event in events if event.get("type") == "final"]
    if not finals:
        raise InspectError("generic events contain no terminal final event")
    final = finals[-1]

    candidates: list[str] = []
    for _, event in events:
        if event.get("type") not in ("session", "final"):
            continue
        if "session_id" not in event:
            if event.get("type") == "session":
                raise InspectError("generic session event has no session_id")
            continue
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise InspectError(
                "generic session_id must be a non-empty string"
            )
        candidates.append(session_id)
    session_id = _single_session_id(candidates, "generic")

    completed = final.get("completed")
    if completed is not True:
        raise InspectError("generic final.completed must be true")
    text = final.get("text")
    if not isinstance(text, str) or not text.strip():
        raise InspectError("generic final event contains no final text")
    return session_id, text


def _generic_cost(
    event: dict[str, Any]
) -> tuple[Any, str, str | None]:
    status = event.get("cost_status")
    scope = event.get("cost_scope")
    if scope is not None and scope not in COST_SCOPES:
        raise InspectError(
            "generic cost_scope must be 'call' or 'session_cumulative'"
        )
    if status is None:
        if event.get("cost") is None:
            return None, "unavailable", None
        raise InspectError("generic cost requires cost_status")
    if status not in COST_STATUSES:
        raise InspectError(
            "generic cost_status must be reported, estimated, or unavailable"
        )
    value = _valid_cost(event.get("cost"))
    if status == "unavailable":
        if value is not None:
            raise InspectError(
                "generic cost_status unavailable requires a null cost"
            )
        return None, "unavailable", None
    if value is None:
        raise InspectError(
            f"generic cost_status {status} requires a valid cost"
        )
    return value, status, scope or "call"


def _generic_call_ratio(
    usage: dict[str, Any], family: str
) -> float | None:
    if family == "included":
        return _ratio(
            usage.get("generic_cached_input_tokens"),
            usage.get("generic_input_tokens"),
        )
    return _ratio(
        usage.get("cache_read"),
        sum(usage.get(key, 0) for key in GENERIC_FAMILY_A_INPUT_KEYS),
    )


def _generic_usage(
    session_id: str,
    events: list[tuple[int, dict[str, Any]]],
    builder_turn: int,
    prior: PriorUsage | None,
    events_path: Path,
) -> dict[str, Any]:
    usage_events = [
        event for _, event in events if event.get("type") == "usage"
    ]
    current_calls: list[dict[str, Any]] = []
    for index, event in enumerate(usage_events, 1):
        scope = event.get("scope")
        if scope is not None and scope != "turn":
            raise InspectError(
                "generic usage scope must be absent or 'turn'"
            )
        raw_usage = event.get("usage")
        if not isinstance(raw_usage, dict):
            raise InspectError("generic usage event requires a usage object")
        family = _generic_family(raw_usage)
        if family is None:
            raise InspectError(
                "generic usage event contains no recognized token fields"
            )
        usage: dict[str, int | float] = {}
        for key in GENERIC_TOKEN_KEYS:
            if key not in raw_usage:
                continue
            value = _as_number(raw_usage[key])
            if value is None or value < 0:
                raise InspectError(
                    f"generic usage field {key} must be a non-negative number"
                )
            usage[key] = value
        if family == "included" and "generic_input_tokens" not in usage:
            raise InspectError(
                "generic included-total usage requires generic_input_tokens"
            )
        cost, cost_status, cost_scope = _generic_cost(event)
        model = event.get("model")
        current_calls.append(
            {
                "builder_turn": builder_turn,
                "turn_call": index,
                "model": model if isinstance(model, str) else None,
                "time": event.get("time"),
                "time_iso": _iso_from_millis(event.get("time")),
                "scope": scope or "turn",
                "usage": usage,
                "cache_read_ratio": _generic_call_ratio(usage, family),
                "cost": cost,
                "cost_status": cost_status,
                "cost_scope": cost_scope,
            }
        )
    current_missing = not current_calls
    return _finalize_usage(
        "generic",
        session_id,
        builder_turn,
        prior,
        current_calls,
        [str(events_path)],
        current_missing,
        0,
        None,
        missing_message=f"no generic usage events for builder turn {builder_turn}",
    )


def inspect(
    host: str,
    events_path: Path,
    final_message_path: Path,
    usage_path: Path,
    builder_turn: int,
    expected_session: str | None = None,
    cwd: Path | None = None,
    handoff_id: str | None = None,
) -> dict[str, Any]:
    if builder_turn < 1:
        raise InspectError("--builder-turn must be a positive integer")
    if builder_turn > 1 and not expected_session:
        raise InspectError("--expected-session is required when --builder-turn > 1")

    events = _read_jsonl(events_path)
    if host == "kimi":
        session_id = _kimi_session_id(events)
        final_message = _kimi_final_message(events)
        if expected_session is not None and session_id != expected_session:
            raise InspectError(
                "Builder session mismatch: expected "
                f"{expected_session} but events report {session_id}"
            )
        prior = _load_prior_usage(usage_path, host, session_id, builder_turn)
        usage = _kimi_usage(
            session_id,
            cwd or Path.cwd(),
            builder_turn,
            prior,
            events_path,
        )
    elif host == "codex":
        session_id = _codex_session_id(events)
        final_message = _codex_final_message(events)
        if expected_session is not None and session_id != expected_session:
            raise InspectError(
                "Builder session mismatch: expected "
                f"{expected_session} but events report {session_id}"
            )
        prior = _load_prior_usage(usage_path, host, session_id, builder_turn)
        usage = _codex_usage(
            session_id,
            events,
            builder_turn,
            prior,
            events_path,
        )
    elif host == "claude-code":
        session_id, final_message, result = _claude_result(events)
        if expected_session is not None and session_id != expected_session:
            raise InspectError(
                "Builder session mismatch: expected "
                f"{expected_session} but events report {session_id}"
            )
        prior = _load_prior_usage(usage_path, host, session_id, builder_turn)
        usage = _claude_usage(
            session_id,
            result,
            builder_turn,
            prior,
            events_path,
        )
    elif host == "dsh":
        session_id, final_message = _dsh_result(events)
        if expected_session is not None and session_id != expected_session:
            raise InspectError(
                "Builder session mismatch: expected "
                f"{expected_session} but events report {session_id}"
            )
        prior = _load_prior_usage(usage_path, host, session_id, builder_turn)
        usage = _dsh_usage(session_id, builder_turn, prior, events_path)
    elif host == "generic":
        session_id, final_message = _generic_session_and_final(events)
        if expected_session is not None and session_id != expected_session:
            raise InspectError(
                "Builder session mismatch: expected "
                f"{expected_session} but events report {session_id}"
            )
        prior = _load_prior_usage(usage_path, host, session_id, builder_turn)
        usage = _generic_usage(
            session_id,
            events,
            builder_turn,
            prior,
            events_path,
        )
    else:
        raise InspectError(f"unsupported host: {host}")

    _atomic_write_text(final_message_path, final_message)
    if handoff_id is not None:
        _check_return_package(final_message, handoff_id)
    _atomic_write_json(usage_path, usage)

    summary: dict[str, Any] = {
        "ok": True,
        "host": host,
        "session_id": session_id,
        "builder_turn": builder_turn,
        "event_count": len(events),
        "usage_status": usage["usage_status"],
        "usage_turn_count": usage["turn_count"],
        "usage_call_count": usage["call_count"],
        "skipped_scope_count": usage["skipped_scope_count"],
        "cache_read_ratio": (usage["totals"] or {}).get("cache_read_ratio"),
        "cache_ratio_denominator": usage["cache_ratio_denominator"],
        "cost_status": usage["cost_status"],
        "cost_scope": usage.get("cost_scope"),
        "events": str(events_path),
        "final_message": str(final_message_path),
        "usage": str(usage_path),
    }
    if usage.get("usage_error"):
        summary["usage_error"] = usage["usage_error"]
    return summary


_REVIEW_MARKERS = (
    "PREWALK_PHASE: NAVIGATOR_REVIEW",
    "PREWALK_PHASE: SOL_REVIEW",
)


def _check_return_package(final_message: str, handoff_id: str) -> None:
    """Reject a final message that is not a Builder-to-Navigator package."""
    if not any(marker in final_message for marker in _REVIEW_MARKERS):
        raise InspectError(
            "final message lacks PREWALK_PHASE: NAVIGATOR_REVIEW return package"
        )
    ids = re.findall(r"^\s*HANDOFF_ID:\s*(\S+)\s*$", final_message, re.M)
    if handoff_id not in ids:
        raise InspectError(
            f"final message HANDOFF_ID mismatch: expected {handoff_id}, found {ids or 'none'}"
        )


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect Prewalk Builder session continuity and usage artifacts"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser(
        "inspect", help="extract session, final message and cumulative usage"
    )
    inspect_parser.add_argument(
        "--host",
        required=True,
        choices=("kimi", "codex", "claude-code", "dsh", "generic"),
    )
    inspect_parser.add_argument("--builder-turn", required=True, type=_positive_int)
    inspect_parser.add_argument("--events", required=True, type=Path)
    inspect_parser.add_argument("--expected-session")
    inspect_parser.add_argument("--final-message", required=True, type=Path)
    inspect_parser.add_argument(
        "--usage",
        required=True,
        type=Path,
        help="cumulative usage artifact; for --builder-turn >1 this file is prior state",
    )
    inspect_parser.add_argument("--cwd", type=Path)
    inspect_parser.add_argument(
        "--handoff-id",
        help="require the final message to be a NAVIGATOR_REVIEW package with this HANDOFF_ID",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        summary = inspect(
            host=args.host,
            events_path=args.events,
            final_message_path=args.final_message,
            usage_path=args.usage,
            builder_turn=args.builder_turn,
            expected_session=args.expected_session,
            cwd=args.cwd,
            handoff_id=args.handoff_id,
        )
    except (InspectError, OSError) as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
