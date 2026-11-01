# Kimi Code adapter

Host id: `kimi`. Use `prewalk_runtime.py inspect --host kimi`.

Kimi Code is one harness binding for the Prewalk core. The core does not pin a Kimi Navigator model; this upstream adapter leaves the Navigator and executor model/effort to the core preflight and the installed manager. A local installation may add its own default-model notes outside this upstream file.

## Load the core instructions

Install `prewalk/` as a Kimi Code skill (`~/.agents/skills/prewalk/` for a user-scope install or `<project>/.agents/skills/prewalk/` for a project). Kimi reads `SKILL.md` and follows the harness-neutral orchestration; this adapter supplies the launch, resume, artifact, and usage details.

## Setup and manager linkage

The optional separate `kimi-deepseek-subagent` skill owns the managed executor configuration: the provider entry, the executor model alias, the effort pin, and the credential check. Use its `status`, `test`, and `repair` commands. Do not hand-edit `config.toml`, copy keys, or substitute a built-in `Agent` tool: the managed surface pins the alias, the effort, and the permission behavior this adapter relies on.

A configured surface is not proof that a requested model or effort works; keep the core model/effort preflight and use the manager's acceptance evidence for the actual pairing.

## Persistent Builder (`kimi-sub`)

The verified path is the persistent ACP backend `kimi-sub`: `prewalk_builder.py` manages the session (`launch`, `execute --wait`, `status [--full]`, `review`, `route`, `approve --wait`, `discard`, `cancel --wait`) and Navigator session usage for the savings report is discovered automatically from the Kimi session whose `wire.jsonl` references the run dir. The `kimi -p` commands below describe the legacy one-shot adapter for non-persistent runs; see [../references/legacy-oneshot.md](../references/legacy-oneshot.md).

## Initial dispatch (legacy one-shot)

Launch from the task workspace and pass the handoff package by absolute file path. Keeping the package out of the command line avoids Windows command-line length limits and quoting corruption; the executor's Read tool loads the full package itself.

```sh
cd "<task workspace>" && kimi -m "<executor-model-alias>" -p "PREWALK_PHASE: BUILDER_EXECUTE
HANDOFF_ID: <HANDOFF_ID>

You are the configured Builder executor at the Builder execution boundary. Your complete, self-contained handoff package is the file \"<absolute handoff package path>\". Read it first with the Read tool, then execute it exactly as written. Your final message must follow the MANDATORY RETURN TO NAVIGATOR section of that package: a concise status followed by one fenced, copy-ready package beginning exactly with PREWALK_PHASE: NAVIGATOR_REVIEW (legacy PREWALK_PHASE: SOL_REVIEW is also accepted during migration) and the same HANDOFF_ID." --output-format stream-json > "<run-dir>/turn-001-events.jsonl" 2> "<run-dir>/turn-001.err"
```

`--output-format stream-json` captures the session metadata and final assistant message as the turn artifact. In `-p` (print) mode the child runs under the `auto` permission policy: it can edit and run commands without approvals, while static deny rules still apply. Do not broaden permissions beyond that on your own initiative.

## Resume

Resume the exact recorded session from the original workspace with the same model/effort boundary. Do not add permission or provider flags that were not part of the recorded launch.

```sh
cd "<original workspace>" && kimi --session "<builder-session-id>" -m "<executor-model-alias>" -p "<short delta prompt that references the absolute delta file>" --output-format stream-json > "<run-dir>/turn-002-events.jsonl" 2> "<run-dir>/turn-002.err"
```

A changed session id or boundary, missing or incompatible prior usage, or a wire/prior call-count disagreement is a failure, not a new baseline. Preserve the work and report the routing/session error; do not cold-start.

## Session, final message, and usage

- Session id: the `session.resume_hint` meta event's `session_id`. Require one consistent id per turn.
- Final message: the last assistant message before that meta event. Do not treat a draft assistant message or process stdout as the final response.
- Usage: the runtime adapter discovers the resumed session through `kimi session list --cwd <workspace> --json` (honoring `KIMI_BIN` and `.cmd`/`.bat` wrapping) and reads `usage.record` entries from `agents/main/wire.jsonl`. Count only records whose `usageScope` is absent or `turn`; exclude other scopes, report the skipped count, and mark the result `partial` rather than double counting. The first `turn.prompt` segment identifies Builder turn 1 when no prior usage exists; resumed turns are sliced by prior earlier-turn call counts.
- Native usage fields are `inputOther`, `inputCacheRead`, `inputCacheCreation`, and `output`; the cache-read ratio denominator is `inputOther+inputCacheRead+inputCacheCreation`. These are native Kimi semantics and are not comparable with another adapter's denominator.
- Cost: record a numeric value or a structured object with a numeric `amount` and optional `currency` only when the session store actually carries it. Otherwise cost is `unavailable`.
- Missing usage is `unavailable`, not zero.

Run the installed runtime adapter after every turn:

```sh
python "<installed-prewalk>/scripts/prewalk_runtime.py" inspect --host kimi --builder-turn <N> --events "<run-dir>/turn-NNN-events.jsonl" --final-message "<run-dir>/turn-NNN-final.txt" --usage "<run-dir>/usage.json" --cwd "<task workspace>"
```

## Validation status

The Kimi launch/resume path, session discovery, and usage extraction were live-validated in prior local Kimi Code runs. This harness-neutral change re-runs the fixture and regression suite but does not repeat that live Kimi acceptance run.
