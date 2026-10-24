# prewalk skill

**prewalk-skill** provides two [Codex skills](https://developers.openai.com/codex/skills/) that run an orchestrated coding handoff:

**Navigator (your main Codex model) → model/effort preflight → prewalk + first credible edit → Builder (DeepSeek-V4.1-Flash, `deepseek-flash`, max reasoning) executes → Navigator reviews the actual diff.**

- `prewalk/` — the orchestration skill: model/effort preflight, Navigator investigation, confidence gate, one atomic edit, judgment-density delegation, dispatch, bounded escalation, and final review.
- `codex-deepseek-subagent/` — the management skill that owns the DeepSeek executor surface: provider config, merged model catalog, credential storage, desktop runtime staging, and acceptance tests.

## Why not `spawn_agent(agent_type="DeepSeek")`

On desktop/CLI builds ≥ 0.146 the collaboration namespace is gated to the ChatGPT backend: a spawned subagent is pinned to the `openai` provider and a non-OpenAI model fails with `The 'deepseek-flash' model is not supported when using Codex with a ChatGPT account.` The older `multi_agent_version: "v1"` catalog override no longer helps (worked around 0.144, dead by 0.146). See [openai/codex#31882](https://github.com/openai/codex/issues/31882) and [openai/codex#36957](https://github.com/openai/codex/issues/36957).

So the executor boundary here is a **managed `codex exec` child process**:

```sh
codex exec --skip-git-repo-check -s workspace-write -C "<workspace>" \
  -m deepseek-flash -c model_provider="deepseek" -c model_reasoning_effort="max" - < "<handoff-package>"
```

This only relies on the top-level `model_provider` override and never touches the collab namespace, so it keeps working across the versions where native dispatch is backend-gated. Sandboxed (`workspace-write`) execution requires the staged runtime to include four sibling files — `codex.exe`, `codex-code-mode-host.exe`, `codex-windows-sandbox-setup.exe`, and `codex-command-runner.exe`; the last one actually launches sandboxed commands, and a staged copy missing it fails every command with `CreateProcessWithLogonW failed: 2` regardless of path shape. See [references/compatibility.md](codex-deepseek-subagent/references/compatibility.md) for the full findings; the work-chain acceptance in `test`/`repair` covers exactly this chain.

## Requirements

- Codex desktop app (Windows or macOS), started at least once
- Python 3.11+ (on Windows the desktop runtime's Python 3.12 works)
- A DeepSeek API key (`sk-...`) — stored in the OS credential vault, never in config files

## Install

Copy both skill directories into your Codex skills location:

- Global: `~/.codex/skills/`
- Or project-only: `<project>/.agents/skills/`

## Setup

1. In Codex, ask: `配置 DeepSeek 执行器` — the management skill runs `setup`, asks for your API key via stdin, writes the `[model_providers.deepseek]` block, the merged model catalog, and stages the desktop runtime.
2. It then runs acceptance: a direct DeepSeek call plus the executor-surface pass phrase (`NATIVE_DEEPSEEK_OK`). Status `ready` means the executor boundary works.
3. For an OpenAI-compatible gateway instead of `https://api.deepseek.com`, run `setup`/`repair` with `--base-url <url>`. Only http/https is accepted, credentials/query/fragment are rejected, and plain http is allowed only for loopback hosts. Without `--base-url`, an existing valid address is preserved; an incompatible hand-written provider still returns `conflict` rather than being overwritten.
4. After changing the parent model, run `repair` to re-apply and re-verify.

## Usage

Ask in plain language, e.g.:

```
[$prewalk] 把这三个批改问题修掉，测试由默认模型进行
```

Navigator runs the prewalk and lands one edit, dispatches the handoff package to the Builder executor process, waits, then reviews the real worktree diff before reporting.

### First use

Prewalk asks the connection questions only when the managed executor is missing, incomplete, or the user wants to change the endpoint: destination base URL (default `https://api.deepseek.com`), API key through the host's secure prompt or stdin, and any protocol/model metadata the host needs for a custom connection. A ready configuration is reused without asking again, and answers are grouped so the user is not interrupted once per run. Configuring a connection does not prove that every requested model or effort works on that surface — model/effort preflight and its validation still apply.

### Live progress

Every task gets one run directory outside the repository containing the handoff, per-turn event/final artifacts, `progress.jsonl`, and stderr; the same directory is reused across resumed turns. Builder appends one JSON event at start, on each TODO transition, before and after long commands, and on completion or escalation; the sharing format is in [prewalk/references/progress-format.md](prewalk/references/progress-format.md). Navigator reports the run directory and background task ID, reads only the new tail at natural checkpoints, and does not busy-poll.

This is a snapshot protocol: progress is self-reported status and raw output remains the tool-level detail. A missing or stale file is not proof of a hung child. The host's background-task panel shows process state, but there is no native live subagent panel in the parent chat; when the parent turn ends, updates pause until the next user message or completion notification. Final proof is still Navigator's inspection of the actual diff.

### Session continuity and usage

Prewalk keeps one resumable Builder session for one task, repository/worktree, branch, model/effort pairing, and permission boundary. The first dispatch is the only full handoff. Later supplements, escalation decisions, and rework resume the recorded session with a small `PREWALK_PHASE: DEEPSEEK_RESUME` delta carrying the unchanged `HANDOFF_ID`, expected Builder session id, new decision/evidence, current TODO state, and progress path — not the original objective and inventory. The protocol is in [prewalk/references/session-continuity.md](prewalk/references/session-continuity.md).

Each run directory holds:

- `run.json` — non-secret routing state: host, session id, workspace/branch, model/effort, permission boundary, next turn, and artifact paths.
- `turn-NNN-events.jsonl` / `turn-NNN.err` — raw per-turn event stream and stderr; never overwritten by a later turn.
- `turn-NNN-final.txt` — final assistant message extracted by the adapter.
- `progress.jsonl` — cumulative Builder progress; `seq` continues across turns and events carry an optional `turn` (Builder turn number, not a model-call index).
- `usage.json` — cumulative schema_version 2 state: `calls` holds raw model-call records with `builder_turn`, model/time/scope/raw usage/ratio and any validated cost; `turns` holds one aggregate per Builder turn with `call_count`, source events, aggregated token counts and ratio. Top-level `call_count` and `turn_count` are separate.

After every turn, run the stdlib-only adapter in `prewalk/scripts/prewalk_runtime.py` with `--builder-turn N` and the same cumulative `usage.json`:

```sh
python prewalk/scripts/prewalk_runtime.py inspect --host kimi --builder-turn 2 --events "<run-dir>/turn-002-events.jsonl" --expected-session "<session-id>" --final-message "<run-dir>/turn-002-final.txt" --usage "<run-dir>/usage.json" --cwd "<workspace>"
```

For `--builder-turn` greater than 1, `--expected-session` and the existing cumulative usage file are required prior state. The adapter extracts the session id and final message, rejects an unexpected session id, rejects schema/session/wire-count mismatches without overwriting outputs, and merges earlier Builder turns. Re-inspecting the same Builder turn replaces its aggregate rather than duplicating it; an older turn is never rewritten once a later one is recorded. Codex usage comes from `turn.completed`; Kimi usage comes from `usage.record` entries in the resumed session's `agents/main/wire.jsonl`, found through `kimi session list --cwd <workspace> --json` (honoring `KIMI_BIN` and `.cmd`/`.bat` wrapping). Kimi counts only `usageScope` absent or `turn`; other scopes are excluded, reported as `partial`, and never double counted. For the initial no-prior Kimi inspection, the first `turn.prompt` segment identifies Builder turn 1; resumed turns are sliced by prior earlier-turn call counts, and a wire shorter than those counts is a non-overwrite failure. Cost is reported only for a numeric value or a structured object with numeric `amount` and optional `currency`; arbitrary text is omitted, and token counts are never converted into an invoice.

Observability limits: a resumable session preserves conversation state but does not guarantee a warm provider cache. Token counts do not prove a cache hit rate or savings, and Kimi and Codex cache ratios use different denominators. Prewalk records the observed evidence and does not claim a general cache improvement. No keepalive prompts or forced compaction are used to chase cache; model/effort changes are preferred at natural compaction or execution boundaries but never postponed when correctness requires them.

## Notes

- Do **not** pick `deepseek-flash` in the desktop model picker: the app persists that choice into the top-level `model` in `config.toml`, which breaks the non-DeepSeek parent contract this setup relies on.
- The API key lives in the OS credential vault (Windows Credential Manager / macOS Keychain) under the target `codex-deepseek-api-key`; no secret is ever written to the repository, config, or logs.
- `--base-url` changes only the provider block this skill manages. Status accepts any structurally valid configured address, not only `api.deepseek.com`.

## Repo layout

```text
prewalk/                  # orchestration skill (SKILL.md, references, runtime adapter, tests)
  scripts/prewalk_runtime.py
  tests/test_prewalk_runtime.py
codex-deepseek-subagent/  # executor management skill (script + docs + tests)
```
