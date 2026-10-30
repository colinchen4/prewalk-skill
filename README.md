# prewalk skill

**prewalk** orchestrates a coding handoff across coding harnesses:

**Navigator (the parent agent on any supported harness) → adapter selection and model/effort preflight → prewalk + first credible edit → Builder executor (a configured harness/model, possibly different from Navigator's) executes → runtime adapter extracts session and usage → Navigator reviews the actual diff.**

The core skill is harness-neutral. It contains no required vendor command, model name, provider, or manager. Host-specific launch, resume, permission, and usage details live in adapter documents under [`prewalk/adapters/`](prewalk/adapters/README.md).

```text
Navigator (parent harness)
  -> prewalk core: orient, confidence gate, one edit, adapter selection
  -> adapters/<host>.md: launch, resume, permissions, artifacts
  -> Builder process on the selected harness/model
  -> scripts/prewalk_runtime.py inspect --host <host>:
       final message, session identity, cumulative usage
  -> Navigator review of the actual worktree diff
```

## Supported adapters

| Host id | Harness | Validation status |
| --- | --- | --- |
| `codex` | Codex desktop/CLI | Launch, resume, and usage live-validated in prior managed runs. Not re-run by the harness-neutral change. |
| `kimi` | Kimi Code | Launch, resume, session discovery, and usage live-validated in prior local runs. Not re-run by the harness-neutral change. |
| `claude-code` | Claude Code 2.1.172 | Installed CLI inspected and an unauthenticated probe rejected; success, resume, and live cost paths are fixture-tested only. |
| `dsh` | DeepSeek Harness | Fixture-tested only; DSH is not installed in this environment. |
| `generic` | Any harness with a thin shim | Normalized contract plus fixture tests. Requires a host-specific shim; not automatic support. |

An adapter's validation status is its own. Fixture-tested is not live-tested, and an adapter for one harness proves nothing about another. Do not describe Prewalk as universally live-tested or as supporting a harness merely because that harness can run a prompt.

## Why adapters

Core orchestration must not assume Codex, Kimi, Claude Code, DSH, a particular model vendor, or one manager's setup contract. Before model/effort dispatch, Navigator selects an adapter and validates its capabilities. Navigator and Builder may use different harnesses and models; the shared contract is the handoff package, phase markers, progress channel, and runtime-extracted artifacts.

The optional repo-root `codex-deepseek-subagent` skill remains an optional host-specific manager for Codex. Core does not require it. See [`prewalk/adapters/codex.md`](prewalk/adapters/codex.md) for its linkage, sandbox constraints, and the reason a managed child process is used instead of Codex's native collaboration namespace.

## Install

Install the `prewalk/` directory as a skill for the harness family you use:

- **Codex:** `~/.codex/skills/` or a project skill directory.
- **Kimi Code:** `~/.agents/skills/prewalk/` or `<project>/.agents/skills/prewalk/`.
- **Claude Code:** `~/.claude/skills/prewalk/` or `<project>/.claude/skills/prewalk/`.
- **DSH:** load the core through its native profile/plugin/prompt mechanism. DSH is plugin-based; do not assume it auto-discovers Agent Skills, and do not invent a plugin manifest.
- **Other harnesses:** keep the native harness and add a thin shim that emits the normalized envelope in [`prewalk/adapters/README.md`](prewalk/adapters/README.md), then use `prewalk_runtime.py inspect --host generic`.

The skill is self-contained for orchestration. The runtime adapter is stdlib-only and needs Python 3.11+. A Builder adapter may additionally depend on a host-specific manager, credentials, or runtime; that dependency belongs to the adapter document, not to core.

## Setup

Quick start is read-only model discovery, not a setup wizard:

```sh
python3 prewalk/scripts/prewalk.py models --navigator-host kimi  # list usable Builder models/efforts; no config writes
```

Prewalk then asks one model/effort preflight: Navigator defaults to the current session's model and effective effort; Builder defaults to an available weak model with `medium` effort when supported, otherwise effort is reported as not settable. `doctor`, `setup`, and `test` are optional diagnostics, not prerequisites. A ready saved config may supply a Builder default but is never required. Backends, blocked models, effort mapping, and what is not yet live-verified: [`prewalk/references/backends.md`](prewalk/references/backends.md).

Handle connection setup only if the selected backend actually fails to launch because authentication or endpoint configuration is missing. A valid existing configuration is reused without asking again; credentials are never written to a handoff, progress file, log, or commit.

Configuring a provider or endpoint is not proof that a requested model or effort works. Keep the core preflight and validate the actual launch values.

## Usage

Ask in plain language, for example:

```text
[$prewalk] 把这三个批改问题修掉，测试由默认模型进行
```

Navigator classifies and plans before any Builder starts, writes L1 only for `sample_guided`, then launches one persistent Builder in an isolated Git worktree. The same PID/session handles scoped checks, implementation, and validation without a cold start. Navigator approves only actual diff and validation evidence; no Builder prose or automatic merge is required. The full protocol is in [`prewalk/references/builder-fusion.md`](prewalk/references/builder-fusion.md).

### First use

Prewalk asks connection questions only when the selected executor is missing, incomplete, or the user wants to change the endpoint. Questions are grouped so the user is not interrupted once per run. A ready configuration is reused without asking again.

### Live progress

Every task gets one run directory outside the repository containing the handoff, per-turn event/final artifacts, `progress.jsonl`, and stderr; the same directory is reused across resumed turns. Builder appends one JSON event at start, on each TODO transition, before and after long commands, and on completion or escalation; the format is in [prewalk/references/progress-format.md](prewalk/references/progress-format.md). Navigator reports the run directory and background task ID, reads only the new tail at natural checkpoints, and does not busy-poll.

This is a snapshot protocol: progress is self-reported status and raw output remains the tool-level detail. A missing or stale file is not proof of a hung child. The host's background-task panel shows process state, but there is no native live subagent panel in the parent chat; when the parent turn ends, updates pause until the next user message or completion notification. Final proof is still Navigator's inspection of the actual diff.

### Session continuity and usage

Prewalk keeps one resumable Builder session for one task, repository/worktree, branch, model/effort pairing, permission boundary, and adapter. The first dispatch is the only full handoff. Later supplements, escalation decisions, and rework resume the recorded session with a small `PREWALK_PHASE: BUILDER_RESUME` delta — legacy `DEEPSEEK_RESUME` is accepted for existing sessions — carrying the unchanged `HANDOFF_ID`, expected Builder session id, new decision/evidence, current TODO state, and progress path. The protocol is in [prewalk/references/session-continuity.md](prewalk/references/session-continuity.md).

Each run directory holds:

- `run.json` — non-secret routing state: `HANDOFF_ID`, Navigator host (`navigator_host`), Builder adapter host (`adapter_host`), session id, workspace/branch, model/effort, permission boundary, next turn, and artifact paths.
- `turn-NNN-events.jsonl` / `turn-NNN.err` — native (or normalized generic) per-turn event stream and stderr; never overwritten by a later turn.
- `turn-NNN-final.txt` — final assistant message extracted by the runtime adapter; do not hand-parse a stream or substitute a draft.
- `progress.jsonl` — cumulative Builder progress; `seq` continues across turns and events carry an optional `turn` (Builder turn number, not a model-call index).
- `usage.json` — cumulative schema_version 2 state: `calls` holds raw model-call records with `builder_turn`, model/time/scope/raw usage/ratio and any validated cost with status and scope; `turns` holds one aggregate per Builder turn with `call_count`, source events, aggregated token counts and ratio. Top-level `call_count` and `turn_count` are separate.

After every turn, run the stdlib-only runtime adapter in `prewalk/scripts/prewalk_runtime.py` with `--host <adapter-host>` and the same cumulative `usage.json`:

```sh
python prewalk/scripts/prewalk_runtime.py inspect --host <adapter-host> --builder-turn 2 --events "<run-dir>/turn-002-events.jsonl" --expected-session "<session-id>" --final-message "<run-dir>/turn-002-final.txt" --usage "<run-dir>/usage.json"
```

For `--builder-turn` greater than 1, `--expected-session` and the existing cumulative usage file are required prior state. The adapter extracts the session id and final message, rejects an unexpected session id, rejects schema/session/wire-count mismatches without overwriting outputs, and merges earlier Builder turns. Re-inspecting the same Builder turn replaces its aggregate rather than duplicating it; an older turn is never rewritten once a later one is recorded. Host-specific usage extraction is documented in each adapter; missing usage means `unavailable`, not zero. Cost is recorded only from a valid host value and is labeled `reported`, `estimated`, or `unavailable`, with scope `call` or `session_cumulative`. A client-side estimate is not an invoice; a session-cumulative estimate is retained as the latest value and never summed across turns.

Observability limits: a resumable session preserves conversation state but does not guarantee a warm provider cache. Token counts do not prove a cache hit rate or savings, and adapters use different denominators. Prewalk records the observed evidence and does not claim a general cache improvement. No keepalive prompts or forced compaction are used to chase cache; model/effort changes are preferred at natural compaction or execution boundaries but never postponed when correctness requires them.

### Adapter details

- [Codex](prewalk/adapters/codex.md)
- [Kimi Code](prewalk/adapters/kimi.md)
- [Claude Code](prewalk/adapters/claude-code.md)
- [DSH](prewalk/adapters/dsh.md)
- [Generic shim contract](prewalk/adapters/README.md)

## Canonical and legacy phase markers

New packages use `PREWALK_PHASE: BUILDER_EXECUTE`, `PREWALK_PHASE: BUILDER_RESUME`, and `PREWALK_PHASE: NAVIGATOR_REVIEW`. Existing sessions and external manager scripts may still use `DEEPSEEK_EXECUTE`, `DEEPSEEK_RESUME`, `LUNA_EXECUTE`, and `SOL_REVIEW`; all are accepted as the corresponding phases. Do not rename markers in scripts outside this skill.

## Notes

- No adapter is presented as universally live-tested. Check the validation-status column and the adapter document before claiming support.
- DSH is plugin-based; its adapter requires the operator to load the core through the native mechanism and is fixture-only here.
- A harness without a shim is not supported by copying `SKILL.md`; use the normalized generic contract and test the shim's session and completion evidence.
- The optional `codex-deepseek-subagent` manager remains at the repository root and is not required by the neutral core.

## Repo layout

```text
prewalk/                       # harness-neutral orchestration skill
  SKILL.md
  adapters/
    README.md                  # adapter contract and generic shim envelope
    codex.md
    kimi.md
    claude-code.md
    dsh.md
    codex/openai.yaml          # Codex host metadata, outside generic core
  references/
    handoff-format.md
    progress-format.md
    session-continuity.md
  scripts/prewalk_runtime.py   # inspect: session, final message, cumulative usage
  tests/test_prewalk_runtime.py
codex-deepseek-subagent/       # optional host-specific Codex manager
```
