# Legacy one-shot orchestration

This reference covers the non-persistent protocol: adapter selection and
validation for hosts without a persistent Builder driver, connection setup,
one-shot launch and session resume, and the legacy Builder-execution and
review checks. The persistent `kimi-sub` path in SKILL.md does not use it.

## Select and validate the adapter

**Discover models without a first-run gate:** run `python3 <skill-dir>/scripts/prewalk.py models --navigator-host <your host>` (read-only). It lists currently usable Builder backends, models, and effort choices from the registry in `scripts/builder_backends/` ([backends.md](backends.md)). It never writes config or launches a test. Do not require `doctor`, `setup`, `test`, or an existing `~/.prewalk/config.json` before Prewalk; those are optional diagnostics and saved-preference tools. A ready saved config may supply a Builder default, but is never a prerequisite.

Adapter selection and capability validation happen before model/effort dispatch:

1. Read [../adapters/README.md](../adapters/README.md) and the selected `adapters/<host>.md`.
2. Identify the Navigator host (the current parent harness) and the Builder host (the configured executor). They may differ.
3. Validate the capabilities the run requires: initial launch, resumable session, session identity, final-message extraction, permission boundary, and native artifact completeness. If a required capability is missing, stop and report it; do not invent a flag, session id, usage value, or final message.
4. Record the Navigator host in `run.json` as `navigator_host` and the Builder adapter host id as `adapter_host`; pass the Builder adapter host id to the runtime adapter as `--host <host>`.

Each backend maps to a runtime host for `prewalk_runtime.py inspect --host`, and adapter files under `adapters/` document those hosts. Never use a Navigator-class model (`opus`, `best`, `fable`, Codex Sol/Astra) as Builder; the registry rejects them. For another harness, the host needs a thin shim that emits the normalized envelope in [../adapters/README.md](../adapters/README.md); copying `SKILL.md` into a directory is not proof of support. Do not infer support for a harness merely because it can execute a prompt, and do not claim an adapter is live-tested when only fixtures exist.

## Connection setup only when needed

Do not make connection setup a separate first-use phase. Start with model discovery and the model/effort preflight. If the selected backend cannot actually launch because authentication or endpoint configuration is missing, report that concrete error and offer the other available models or the backend's documented managed setup path. Never ask for credentials on every run, repeat a valid setup, or run connection setup during Builder execution, Navigator review, or resume from a phase marker.

When setup is genuinely needed, ask only for the missing information through the host's secure mechanism. Never echo credentials or put them in a handoff, progress file, log, or commit. Apply changes only through the adapter's documented managed configuration path; never hand-edit TOML, JSON, credentials, or runtime copies. Configuring a provider or endpoint is not proof that a requested model or effort works: keep the model/effort preflight and its validation.

## Legacy dispatch (async detached mode)

Serial blocking dispatch is the default. Only when the user asks for it, or Navigator has independent work while Builder runs, follow [async-mode.md](async-mode.md): before the first edit, capture the inherited worktree as L0 with `prewalk_layers.py snapshot --layer L0`; then make the first edit and launch with a short brief through `scripts/prewalk_async.py`. Keep working, read only the new progress tail with `status --after`, redirect with `steer`, stop with `cancel`. Final review rules are unchanged.

## Resume the Builder session (legacy)

When continuing the same task and boundary — a supplement, an escalation decision, or rework — do not start another Builder session. Read [session-continuity.md](session-continuity.md), load `run.json`, and send only a `PREWALK_PHASE: BUILDER_RESUME` delta from [handoff-format.md](handoff-format.md). Legacy `PREWALK_PHASE: DEEPSEEK_RESUME` deltas are accepted for existing sessions. The delta repeats the unchanged `HANDOFF_ID` and expected Builder session id and carries only the new decision, new evidence or constraints, current TODO state, progress path, and the normal `NAVIGATOR_REVIEW` return requirement. Do not resend the original objective, repository inventory, old evidence, first edit, or full handoff.

Launch the resume from the original workspace with the selected adapter's documented resume command. Do not add model, effort, provider, or permission flags that were not part of the recorded launch; repeat only the grants the adapter documents as required on resume. Capture a new turn artifact and never overwrite earlier turns. Then run the runtime adapter with `--host "<adapter-host>" --builder-turn "<next-turn>" --expected-session "<builder-session-id>" --usage "<run-dir>/usage.json"`; for turn 2 and later the same cumulative usage file is required prior state. A changed session id, missing or incompatible prior usage, or a host-specific artifact disagreement is a failure, not a new baseline. Confirm the inherited workspace and model/effort boundary from the launch and usage evidence; a mismatch means the resume crossed a boundary and must be reported. A resume failure must not silently cold-start: preserve the work and report the routing/session error. Cold recovery requires explicit user authorization and a recorded reason, and starts a new Builder session. Start a different session for a different objective, repository/worktree, branch, model/effort/provider, or permission boundary. Prefer a model or effort change at an observed compaction or execution boundary where context must be rebuilt anyway, but never postpone a correctness-required change to protect cache. Do not invent forced-compaction commands or send keepalive prompts.

## Phase B: Builder execution (legacy one-shot)

### Activate executor mode

Treat `PREWALK_PHASE: BUILDER_EXECUTE`, `PREWALK_PHASE: BUILDER_RESUME`, and the accepted legacy markers `PREWALK_PHASE: DEEPSEEK_EXECUTE`, `PREWALK_PHASE: DEEPSEEK_RESUME`, and `PREWALK_PHASE: LUNA_EXECUTE` as explicit cancellation of Navigator's stop boundary. Retain the incoming `HANDOFF_ID` unchanged for the return package. Do not invoke Navigator prewalk again. On the initial turn, inspect worktree status, the retained first diff, and the handed-off evidence before reopening files; on a resumed turn, inspect current worktree state and follow only the new delta within the existing context.

Adopt the handed-off TODO list. Re-plan only when repository evidence invalidates it; record the reason and keep the list bounded.

### Publish progress

Read [progress-format.md](progress-format.md) and the handoff's `PROGRESS CHANNEL` before inspecting files. Append the first event to the stated `progress.jsonl` before touching the repository, then keep appending one complete UTF-8 JSON line — flushed immediately — when a TODO starts, completes, or is replanned; before and after long builds and tests or a meaningful batch of edits; and when blocked, escalated, or complete. On a resumed turn, append to the same progress file and continue `seq`; set the optional `turn` field to the Builder turn number. Include the sequence number, state, short summary, current TODO, known changed paths, validation status, and next step. Never include secrets, environment files, tokens, hidden reasoning, or raw command output.

The progress channel is a snapshot for Navigator, not a completion signal: keep working and validating normally, and report the final status and last sequence in the return package.

### Execute and verify

Complete the remaining implementation, debugging, focused tests, and appropriate regression checks. Preserve the first edit when sound; revise it when validation shows it is wrong. Track all changed paths and commands. Do not stop merely because the handoff's first suggestion fails. The mechanical work is yours even when it is hard: compile errors, failing or flaky tests, and unclear library behavior are problems to diagnose, not reasons to escalate.

Before finishing:

- Inspect the complete diff against the inherited baseline.
- Check requirements, edge cases, compatibility, and unintended changes.
- Run the strongest practical focused and regression validation.
- Mark TODO items accurately; leave unresolved work explicit.

### Return to Navigator (legacy one-shot path)

Persistent Builders return process metadata, complete diffs, and validation artifacts; they do not return a prose acceptance package. The following applies only to a legacy one-shot adapter.

Read [handoff-format.md](handoff-format.md). Return a concise completion status followed by one fenced, copy-ready package using the **Builder to Navigator** template. Generate it whether execution completed, became blocked, or was escalated. The package begins with `PREWALK_PHASE: NAVIGATOR_REVIEW`; the legacy `PREWALK_PHASE: SOL_REVIEW` remains accepted during migration.

Use the runtime adapter's extracted final message as the turn's final assistant response. Do not hand-parse a native stream or substitute a draft message for it.

Include the original objective, inherited first edit, complete changed-path inventory, implementation decisions, exact validation results, unresolved risks, and the diff range or baseline Navigator should review. Carry the model/effort selection forward exactly as recorded in the handoff, note any deviation you observed, and do not assert your own model identity beyond those recorded values. Record which decisions Navigator retained and did not hand off, and if you escalated, the exact decision required with the evidence that forced it. Include the final progress status and last sequence, and do not perform the Navigator review yourself.

## Legacy review checks

For a legacy run, start from `review.md` built from Git layers, not Builder prose. Inspect `run.json` and that turn's `prewalk_runtime.py inspect` output, including its return marker, session identity, and cumulative usage. A missing or stale progress file is not proof of failure or of a hung process: inspect the background task and raw output before diagnosing, and report the last event time rather than inventing activity. Treat all usage and cost records as observability only, never proof of quality or model identity.
