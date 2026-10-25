# Builder session continuity and usage

Keep one resumable Builder session for one objective, repository/worktree, branch, model/effort pairing, permission boundary, and adapter. A process may exit; persistence means resuming the same host session, not keeping an idle process alive. Never run two processes against the same Builder session concurrently.

## Run record and turns

Keep the run directory outside the repository. Use `run.json` for non-secret routing state: `HANDOFF_ID`, Navigator host (`navigator_host`), Builder adapter host id (`adapter_host`), Builder session ID, repository/worktree, branch, model/effort, permission boundary, next turn number, and artifact paths. Do not store API keys, full event logs, or conversation history there.

The initial turn uses the full Navigator-to-Builder handoff. Name artifacts by turn (`turn-001-events.jsonl`, `turn-001.err`, `turn-001-final.txt`), while retaining the shared `progress.jsonl` and cumulative `usage.json`. Never overwrite an earlier turn's raw events or final response. A Builder turn is one process invocation; a model call is one provider usage record. Never label model calls as Builder turns.

After every turn, run the installed `scripts/prewalk_runtime.py inspect` adapter with `--host <adapter-host> --builder-turn N`, the turn's event artifact, and, on a resumed turn, the expected session ID. The runtime adapter extracts the final assistant message and session ID from the native or normalized artifact; do not hand-parse a stream and do not substitute a draft message. It must reject a changed session ID, preserve earlier turns, and write usage metrics only when the host exposes them. A final response or session ID may not be inferred from a process's self-description alone. Read `adapters/<host>.md` for that host's artifact and completeness limits.

## Resume with a delta

For a supplement, escalation decision, or rework on the same task and boundary, resume the exact recorded Builder session through the selected adapter. Send only a `PREWALK_PHASE: BUILDER_RESUME` delta containing:

- the unchanged `HANDOFF_ID` and expected `BUILDER_SESSION_ID`;
- the new Navigator decision or requested correction;
- new evidence and changed constraints only;
- current TODO state and progress-channel path;
- the unchanged requirement to return `PREWALK_PHASE: NAVIGATOR_REVIEW` (legacy `SOL_REVIEW` accepted) with the same handoff ID.

Legacy `PREWALK_PHASE: DEEPSEEK_RESUME` deltas are accepted for existing sessions; do not rename markers in manager scripts outside this skill.

Do not resend the original objective, repository inventory, old evidence, first edit, or full handoff. If the resumed context is unavailable or inconsistent, stop and report; do not restate the full handoff or cold-start without explicit user authorization. On resume, Builder continues from its existing context, checks current worktree state, and appends progress using the next sequence number. It does not repeat Navigator prewalk or connection setup. Repeat only the permission/approval/add-directory grants that the adapter documents as required on resume.

Do not silently start a fresh Builder after a resume failure. Preserve the work and report the routing/session error. A cold-recovery full handoff requires explicit user authorization and a recorded reason; it is a new Builder session, not a successful resume. An expired provider cache alone is not a reason to discard a valid resumable session.

Start a different Builder session for a different objective, repository/worktree, branch, model/effort/provider, permission boundary, or adapter; that is a new task or boundary, not cold recovery. When a model or effort change is needed, prefer an observed compaction or other execution boundary where context must be rebuilt anyway; never postpone a correctness-required change merely to protect cache. Do not invent a host command to force compaction.

## Cache-friendly context

Keep stable role and execution instructions before variable run metadata. Never rewrite earlier prompts or inject changing progress into the stable prefix. Append delta turns instead. Progress and usage artifacts stay outside the model conversation unless a specific snapshot is needed.

A resumable session preserves conversation state but does not guarantee a warm provider cache. Reduce unnecessary idle gaps, do not send keepalive prompts solely to chase cache hits, and measure the provider's actual TTL and behavior. Switching models does not transfer one model's cache to another.

## Usage and cost evidence

Use `prewalk_runtime.py inspect --host <adapter-host> --builder-turn N` after each completed turn. `usage.json` is schema_version 2 and cumulative across resumed turns:

- `calls` holds the raw model-call usage records with call index, `builder_turn`, model/time/scope/raw usage, adapter-specific ratio, and any validated cost with `cost_status` and `cost_scope`.
- `turns` holds one aggregate per Builder turn with `turn`, `call_count`, source events, aggregated token counts, and ratio.
- Top-level totals cover every call; `call_count` and `turn_count` are reported separately.
- Re-inspecting the same Builder turn replaces that turn's aggregate rather than duplicating it. Do not rewrite an older turn when a later turn is already recorded. A schema, session, or artifact/prior-count mismatch is a non-overwrite failure.
- `cache_ratio_denominator` labels the token denominator used by that adapter. Native token semantics differ between hosts; do not compare ratios across adapters without their labels, and do not claim a cache benefit from a ratio alone.

Usage extraction is adapter-specific. Read `adapters/<host>.md` for the native event shape, discovery method, scopes, and denominator. The shared rule is: count only records the adapter can prove belong to this Builder turn, exclude other scopes and report the skipped count rather than double counting, and never infer usage from reasoning or tool events.

Record provider-reported cost only when the host actually returns a numeric value or a structured object with a numeric `amount` and optional `currency`. Use `cost_status`:

- `reported` for a provider-reported charge;
- `estimated` for a client-side estimate, which is not an invoice;
- `unavailable` when no trustworthy value exists.

Use `cost_scope`: `call` for a per-call value, `session_cumulative` for a value that already covers earlier calls in the same session. Across resumed turns, retain the latest `session_cumulative` estimate; never sum it across calls or turns. Otherwise write `cost: null` and `cost_status: "unavailable"`; token counts alone are not an invoice. Do not fabricate prices, cache hits, reasoning tokens, or savings. Missing usage is an observability gap, not proof of zero cache or zero cost.
