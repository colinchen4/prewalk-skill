# Builder session continuity and usage

Keep one resumable Builder session for one objective, repository/worktree, branch, model/effort pairing, and permission boundary. A process may exit; persistence means resuming the same host session, not keeping an idle process alive. Never run two processes against the same Builder session concurrently.

## Run record and turns

Keep the run directory outside the repository. Use `run.json` for non-secret routing state: `HANDOFF_ID`, host, Builder session ID, repository/worktree, branch, model/effort, permission boundary, next turn number, and artifact paths. Do not store API keys, full event logs, or conversation history there.

The initial turn uses the full Navigator-to-Builder handoff. Name artifacts by turn (`turn-001-events.jsonl`, `turn-001.err`, `turn-001-final.txt`), while retaining the shared `progress.jsonl` and cumulative `usage.json`. Never overwrite an earlier turn's raw events or final response. A Builder turn is one process invocation; a model call is one provider usage record. Never label model calls as Builder turns.

After every turn, run the installed `scripts/prewalk_runtime.py inspect` adapter with `--builder-turn N`, the event file, and, on a resumed turn, the expected session ID. For N>1 the existing cumulative `usage.json` is required prior state. The adapter must extract the final assistant message and session ID, reject a changed session ID, preserve earlier turns, and write usage metrics when the host exposes them. A final response or session ID may not be inferred from a process's self-description alone.

## Resume with a delta

For a supplement, escalation decision, or rework on the same task and boundary, resume the exact recorded Builder session. Send only a `PREWALK_PHASE: DEEPSEEK_RESUME` delta containing:

- the unchanged `HANDOFF_ID` and expected `BUILDER_SESSION_ID`;
- the new Navigator decision or requested correction;
- new evidence and changed constraints only;
- current TODO state and progress-channel path;
- the unchanged requirement to return `PREWALK_PHASE: SOL_REVIEW` with the same handoff ID.

Do not resend the original objective, repository inventory, old evidence, first edit, or full handoff. If the resumed context is unavailable or inconsistent, stop and report; do not restate the full handoff or cold-start without explicit user authorization. On resume, Builder continues from its existing context, checks current worktree state, and appends progress using the next sequence number. It does not repeat Navigator prewalk or connection setup.

Do not silently start a fresh Builder after a resume failure. Preserve the work and report the routing/session error. A cold-recovery full handoff requires explicit user authorization and a recorded reason; it is a new Builder session, not a successful resume. An expired provider cache alone is not a reason to discard a valid resumable session.

Start a different Builder session for a different objective, repository/worktree, branch, model/effort/provider, or permission boundary; that is a new task or boundary, not cold recovery. When a model or effort change is needed, prefer an observed compaction or other execution boundary where context must be rebuilt anyway; never postpone a correctness-required change merely to protect cache. Do not invent a host command to force compaction.

## Cache-friendly context

Keep stable role and execution instructions before variable run metadata. Never rewrite earlier prompts or inject changing progress into the stable prefix. Append delta turns instead. Progress and usage artifacts stay outside the model conversation unless a specific snapshot is needed.

A resumable session preserves conversation state but does not guarantee a warm provider cache. Reduce unnecessary idle gaps, do not send keepalive prompts solely to chase cache hits, and measure the provider's actual TTL and behavior. Switching models does not transfer one model's cache to another.

## Usage and cost evidence

Use `prewalk_runtime.py inspect --builder-turn N` after each completed turn. `usage.json` is schema_version 2 and cumulative across resumed turns:

- `calls` holds the raw model-call usage records with call index, `builder_turn`, model/time/scope/raw usage, ratio, and any validated cost.
- `turns` holds one aggregate per Builder turn with `turn`, `call_count`, source events, aggregated token counts, and ratio.
- Top-level totals cover every call; `call_count` and `turn_count` are reported separately.
- Re-inspecting the same Builder turn replaces that turn's aggregate rather than duplicating it. Do not rewrite an older turn when a later turn is already recorded. A schema, session, or wire/prior-count mismatch is a non-overwrite failure.

Kimi usage comes from `usage.record` entries in the resumed session's `agents/main/wire.jsonl`, discovered through `kimi session list --cwd <workspace> --json`; discovery honors `KIMI_BIN` and `.cmd`/`.bat` wrapping. Read only the usage records; do not edit session storage or copy conversation content into the report. Count only records whose `usageScope` is absent or `turn`; exclude other scopes, report the skipped count, and mark the result `partial` rather than double counting. The first `turn.prompt` segment identifies Builder turn 1 when no prior usage exists; resumed turns are sliced by prior earlier-turn call counts, and a wire shorter than those counts is a non-overwrite failure. Codex usage comes from `turn.completed` events in `codex exec --json` output, merged with compatible prior usage; `cached_input_tokens` remains included in `input_tokens`.

Record provider-reported cost only when the host actually returns a numeric value or a structured object with a numeric `amount` and optional `currency`. Ignore arbitrary strings or malformed cost objects. Otherwise write `cost: null` and `cost_status: "unavailable"`; token counts alone are not an invoice. Do not fabricate prices, cache hits, reasoning tokens, or savings. Missing usage is an observability gap, not proof of zero cache or zero cost.
