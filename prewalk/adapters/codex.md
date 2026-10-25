# Codex adapter

Host id: `codex`. Use `prewalk_runtime.py inspect --host codex`.

Codex is one harness binding for the Prewalk core. Nothing in `SKILL.md` requires Codex, its collaboration namespace, or the optional manager below.

## Load the core instructions

Install `prewalk/` where Codex loads skills (`~/.codex/skills/` or a project skill directory). Codex reads `SKILL.md` and follows the harness-neutral orchestration; this adapter supplies the launch, resume, artifact, and usage details.

## Setup and manager linkage

The optional repo-root `codex-deepseek-subagent` skill owns the executor surface: the provider block, merged model catalog, credential storage, desktop runtime staging, and acceptance tests. Use its `status`, `test`, and `repair` commands. Do not hand-edit TOML, JSON, credentials, or runtime copies. `CODEX_HOME` must point at the Codex home that holds that managed configuration.

A configured endpoint is not proof that a requested model or effort works; keep the core model/effort preflight and use the manager's acceptance evidence for the actual pairing.

Do not pick the executor model in the desktop model picker: the app persists that choice into the top-level `model` in `config.toml` and can break the non-executor parent contract this setup relies on.

## Why not `spawn_agent`

On desktop/CLI builds >= 0.146 the collaboration namespace is gated to the ChatGPT backend: a spawned subagent is pinned to the `openai` provider and a non-OpenAI model fails with a model-unsupported 400. The older `multi_agent_version: "v1"` catalog override no longer helps. See [openai/codex#31882](https://github.com/openai/codex/issues/31882) and [openai/codex#36957](https://github.com/openai/codex/issues/36957). The executor boundary is therefore a managed `codex exec` child process, which uses only the top-level provider override.

## Initial dispatch

Launch from the task workspace with a writable sandbox so the child can edit the repository. Substitute the model/provider/effort values validated by the core preflight; the manager's default pairing is the DeepSeek-Flash surface when that is what the user selected.

```sh
cd "<task workspace>" && CODEX_HOME="$CODEX_HOME" "<staged-codex.exe>" exec --skip-git-repo-check -s workspace-write -C "<task workspace>" -m "<executor-model>" -c model_provider="\"<provider>\"" -c model_reasoning_effort="\"<effort>\"" --json - < "<run-dir>/turn-001-handoff.txt" > "<run-dir>/turn-001-events.jsonl" 2> "<run-dir>/turn-001.err"
```

Keep `-s workspace-write`: the sandbox is part of this adapter's permission boundary. Paths with spaces work when the staged runtime bundle is complete. The bundle must contain `codex.exe`, `codex-code-mode-host.exe`, `codex-windows-sandbox-setup.exe`, and `codex-command-runner.exe`; the last one launches sandboxed commands, and a staged copy missing it fails every command with `CreateProcessWithLogonW failed: 2`. Do not switch to `danger-full-access` or otherwise broaden permissions on your own initiative; ask the user once if a different boundary is genuinely required.

## Resume

Resume the exact recorded session from the original workspace. Do not add sandbox, model, effort, or provider flags: the resumed session inherits its recorded boundary.

```sh
cd "<original workspace>" && CODEX_HOME="$CODEX_HOME" "<staged-codex.exe>" exec resume --json "<builder-session-id>" - < "<run-dir>/turn-002-delta.txt" > "<run-dir>/turn-002-events.jsonl" 2> "<run-dir>/turn-002.err"
```

A resume failure is a routing/session failure: preserve the work, report it, and do not silently cold-start.

## Session, final message, and usage

- Session id: `thread.started.thread_id`. Require one consistent id per turn.
- Final message: the last `item.completed` event whose `item.type` is `agent_message`; read `item.text`. Do not treat other item types or process stdout as the final response.
- Usage: `turn.completed.usage`. The native fields are `input_tokens`, `cached_input_tokens`, `output_tokens`, and `reasoning_output_tokens`. `cached_input_tokens` is included in `input_tokens`, so the cache-read ratio uses denominator `input_tokens`. This is a native-token semantic; do not compare it with a denominator from another adapter.
- Cost: record a numeric value or a structured object with a numeric `amount` and optional `currency` only when the stream actually carries it. Otherwise cost is `unavailable`; token counts are not an invoice.
- Missing `turn.completed` usage means `unavailable`, not zero.

Run the installed runtime adapter after every turn:

```sh
python "<installed-prewalk>/scripts/prewalk_runtime.py" inspect --host codex --builder-turn <N> --events "<run-dir>/turn-NNN-events.jsonl" --final-message "<run-dir>/turn-NNN-final.txt" --usage "<run-dir>/usage.json"
```

## Validation status

The Codex launch/resume path and its usage extraction were live-validated in prior Windows desktop runs with the managed executor. This harness-neutral change re-runs the fixture and regression suite but does not repeat that live Codex acceptance run.
