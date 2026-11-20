# Devin CLI adapter

Host id: `devin`. Use `prewalk_runtime.py inspect --host devin` (or persistent backend `devin-sub`).

Devin CLI is one harness binding for the Prewalk core. Core orchestration remains harness-neutral; this adapter supplies Devin's installation path, auth/environment rules, persistent ACP launch/routing contract, and usage extraction.

## Load the core instructions

Install `prewalk/` as a Devin skill:

- **Windows (global):** `%APPDATA%\devin\skills\prewalk\` (typically `C:\Users\<user>\AppData\Roaming\devin\skills\prewalk\`)
- **macOS / Linux (global):** `~/.config/devin/skills/prewalk/`
- **Project-specific:** `<project>/.devin/skills/prewalk/`

Devin reads `SKILL.md` and follows the harness-neutral orchestration; this adapter supplies the Devin-specific `devin-sub` persistent Builder details.

## Recommended Devin model pairing

- **Navigator:** your parent Devin session model (for example `gemini-4-neon-high`).
- **Builder (`devin-sub`):** `swe-2-high` (default effort `medium`) or `swe-2-flash-medium`.
- **Blocked as Builder:** Navigator-class and high-cost models (`neon`, `argon`, `carbon`, `hydrogen`, `opus`, `fable`, `sol`, `astra`, `fusion`, `adaptive`, `lightning`, `pro`, `ultra`). Never run a Navigator-class model as Builder.

## Auth and environment isolation

`devin-sub` reuses your existing `devin auth login` credentials (Devin account OAuth). Prewalk never reads, stores, or forwards tokens or API keys.

When `devin acp` or `devin auth status` is spawned from inside a Devin Desktop / Windsurf shell, the parent environment often sets `ACP_BACKEND=windsurf` (and `WINDSURF_*` / `DEVIN_*` overrides), which causes `devin` to ignore stored CLI credentials and fail with `-32000 Authentication required`. `devin-sub` strips `ACP_BACKEND`, `WINDSURF_API_KEY`, `WINDSURF_IDE_TYPE`, `WINDSURF_EXT_HOST_PID`, `DEVIN_MODEL`, `DEVIN_PERMISSION_MODE`, `DEVIN_REFUSAL_FALLBACK`, and `DEVIN_VERBOSITY` from probe and Builder child environments so stored CLI OAuth works reliably.

Binary discovery checks `DEVIN_BIN`, `PATH` (`devin`), and on Windows falls back to `%LOCALAPPDATA%\Programs\Devin\resources\app\extensions\windsurf\devin\bin\devin.exe`.

## Persistent Builder (`devin-sub`)

The verified path is the persistent ACP backend `devin-sub`: `prewalk_builder.py` launches `devin acp` over stdio and manages one persistent PID/session on an isolated Git worktree (`launch`, `execute --wait`, `status [--full]`, `review`, `route`, `approve --wait`, `discard`, `cancel --wait`).

```sh
python "<installed-prewalk>/scripts/prewalk_builder.py" launch --run-dir "<run-dir>" --plan "<run-dir>/plan.json" --backend devin-sub
python "<installed-prewalk>/scripts/prewalk_builder.py" execute --run-dir "<run-dir>" --text "<short execute delta>" --wait
# inspect builder-review.patch, builder-review.json, validation/
python "<installed-prewalk>/scripts/prewalk_builder.py" approve --run-dir "<run-dir>" --wait
```

### ACP model, thought level, and permissions

- `devin acp` exposes `configOptions` with `id="model"` (family representative values such as `swe-2-high` for SWE-2 and `swe-2-flash-medium` for SWE-2 Flash) and `id="thought_level"` (`medium`, `high`, `max`).
- `devin-sub` maps requested models/efforts onto accepted ACP options: e.g., `swe-2-high` with `--effort medium` sets `model="swe-2-high"` and `thought_level="medium"` (effective model `swe-2-medium`); `swe-2-flash-medium` sets `model="swe-2-flash-medium"` and `thought_level="medium"`.
- If `plan.json` omits `--model`, `devin-sub` defaults explicitly to `swe-2-high` / `medium` so it never inherits a user's default Fusion or Frontier model from `config.json`.
- `session/set_mode` sets `modeId="bypass"` inside the isolated Prewalk worktree so non-interactive tool calls and validation runs do not deadlock waiting on interactive approval prompts. All changes remain isolated to the detached worktree until Navigator inspects the diff and runs `approve`.
- Mid-run model routing (`route --compaction-id <id> --model swe-2-high --effort high` or `--model swe-2-flash-medium`) calls `session/set_config_option` on the same live ACP session and PID — no cold start.

## Session, final message, and usage

- **Session id:** returned by `session/new` (`result.sessionId`) and checked across every `session/prompt` turn.
- **Final message:** concatenated `session/update` `agent_message_chunk` text for the turn ending with `stopReason: "end_turn"`.
- **Usage:** extracted from `_cognition.ai/turn_stats` correlated by `turnClientMessageId == result._meta["cognition.ai/userMessageId"]`. Each turn's `responseDimensions` reports cumulative turn totals (`input_tokens`, `output_tokens`, `cached_input_tokens`) and the active `model` label (normalized to canonical slugs such as `swe-2-medium` or `swe-2-flash-medium`). Model-call count comes from non-subagent `session/update` `usage_update` notifications (skipping duplicate `cognition.ai/subagent_context` mirrors), falling back to `agent_messages`.
- **Cache denominator:** Devin reports `cached_input_tokens` as included in `input_tokens`; `inputOther = input_tokens - cached_input_tokens`, `inputCacheRead = cached_input_tokens`, and `cache_ratio_denominator = "input_total"`.
- **Cost:** native ACP events do not emit billing charges; `cost` is `null` and `cost_status` is `"unavailable"`.
- **Optional savings report:** off by default to avoid extra Navigator token cost. If opted in (`--savings`, `PREWALK_SAVINGS=1`, or `setup --savings`), record the Navigator model before approving (`prewalk_builder.py record-usage --run-dir <run-dir> --model gemini-4-neon-high`) and provide model rates in `~/.prewalk/prices.json`.

## Legacy one-shot (`devin -p`)

Non-persistent `devin --model <effective> --permission-mode dangerous --respect-workspace-trust false -p ...` (and `--resume <session>`) is exposed by `DevinSub.launch()` for registry completeness, but the persistent ACP path (`devin-sub` via `prewalk_builder.py`) is the supported path.

## Validation status

- Unit/fixture tests cover binary discovery, auth status parsing, blocked models, ACP `model`/`thought_level`/`bypass` negotiation, mid-run `route`, `_cognition.ai/turn_stats` usage extraction, and host auto-detection.
- Live Devin ACP verification covers persistent session launch, `swe-2-high` and `swe-2-flash-medium` model/thought-level switching on one PID/session, isolated execution, independent validation, usage collection, review, and approval.
