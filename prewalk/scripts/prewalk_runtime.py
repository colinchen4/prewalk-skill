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


def _add_input_totals(host: str, totals: dict[str, Any]) -> None:
    if host == "kimi":
        if any(key in totals for key in KIMI_INPUT_KEYS):
            input_total = sum(totals.get(key, 0) for key in KIMI_INPUT_KEYS)
            totals["input_total"] = input_total
            totals["cache_read_ratio"] = _ratio(
                totals.get("inputCacheRead"), input_total
            )
    elif "input_tokens" in totals:
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
    token_keys = KIMI_TOKEN_KEYS if host == "kimi" else CODEX_TOKEN_KEYS
    totals = _sum_known(usage_fields, token_keys)
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
    token_keys = KIMI_TOKEN_KEYS if host == "kimi" else CODEX_TOKEN_KEYS
    totals = _sum_known(usage_fields, token_keys)
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

    costs: list[dict[str, Any]] = []
    for call in calls_out:
        value = _valid_cost(call.get("cost"))
        if value is None:
            value = _extract_cost(call)
        call["cost"] = value
        if value is not None:
            costs.append(
                {
                    "builder_turn": call["builder_turn"],
                    "call": call["call"],
                    "value": value,
                }
            )

    if not costs:
        cost_value: Any = None
        cost_status = "unavailable"
    elif len(costs) == 1:
        cost_value = costs[0]["value"]
        cost_status = "reported"
    else:
        cost_value = costs
        cost_status = "reported"

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
        "cache_ratio_denominator": (
            "inputOther+inputCacheRead+inputCacheCreation"
            if host == "kimi"
            else "input_tokens"
        ),
        "cost": cost_value,
        "cost_status": cost_status,
        "source": source_out,
    }


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


def inspect(
    host: str,
    events_path: Path,
    final_message_path: Path,
    usage_path: Path,
    builder_turn: int,
    expected_session: str | None = None,
    cwd: Path | None = None,
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
    else:
        raise InspectError(f"unsupported host: {host}")

    _atomic_write_text(final_message_path, final_message)
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
        "cost_status": usage["cost_status"],
        "events": str(events_path),
        "final_message": str(final_message_path),
        "usage": str(usage_path),
    }
    if usage.get("usage_error"):
        summary["usage_error"] = usage["usage_error"]
    return summary


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
    inspect_parser.add_argument("--host", required=True, choices=("kimi", "codex"))
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
