---
name: prewalk
description: "Run an orchestrated coding handoff: Navigator (the parent agent on the selected harness) investigates and lands one credible edit, selects a harness adapter, dispatches a configured Builder executor (possibly on a different harness or model vendor) to complete mechanical implementation, builds/tests, and debugging at the Builder execution boundary, waits for it, then Navigator reviews the actual final diff. Also resume execution or review from PREWALK_PHASE markers, resume the same Builder session with a delta, including an escalated executor return that asks Navigator for a decision. Use when the user asks for Prewalk, Navigator-to-Builder execution, a Builder executor with Navigator review, frontier-model preparation before delegated coding, execution from a Prewalk package, or final review of returned executor work."
---

# Prewalk

Transfer an evidence-backed coding trajectory from Navigator through a configured Builder executor and back to Navigator review. Do not substitute a prose plan for contact with the code.

## Roles and harness independence

- **Navigator** is the parent agent that keeps the plan, requirement and ambiguity interpretation, architecture and correctness-critical choices, and final review. It runs on whatever harness and model the user selected for the parent session.
- **Builder** is the execution role. It implements, debugs, validates, and reports; it does not perform the final review. A host adapter determines how Builder is launched, resumed, and observed.
- Core orchestration is harness-neutral. Navigator and Builder may use different harnesses, model vendors, and models. The shared contract is the handoff package, phase markers, progress channel, and runtime-extracted session/usage artifacts — not a particular vendor command.

## Select the run mode

Choose the mode from the incoming request:

- **Orchestrated Navigator run:** Use when no phase marker exists. Run Navigator prewalk, select an adapter and dispatch Builder, wait, then perform Navigator review in the same parent task.
- **Builder execution:** Use when the prompt begins with `PREWALK_PHASE: BUILDER_EXECUTE`. Legacy `PREWALK_PHASE: DEEPSEEK_EXECUTE` and `PREWALK_PHASE: LUNA_EXECUTE` identify the same phase and remain accepted for existing sessions. Complete the executor phase and return a Navigator review package.
- **Builder resume:** Use when the prompt begins with `PREWALK_PHASE: BUILDER_RESUME`. Legacy `PREWALK_PHASE: DEEPSEEK_RESUME` identifies the same phase and remains accepted. Continue the recorded Builder session at the same boundary; do not rerun Navigator prewalk or start a new session.
- **Navigator review:** Use when the incoming prompt begins with `PREWALK_PHASE: NAVIGATOR_REVIEW`. Legacy `PREWALK_PHASE: SOL_REVIEW` identifies the same phase and remains accepted.

Legacy markers exist for compatibility with sessions and external manager scripts that predate the neutral names. Do not rename markers in manager scripts outside this skill. Never apply the stop-after-one-edit boundary to Builder execution, and never repeat Navigator investigation in executor mode unless retained evidence is contradicted by the repository.

## Select and validate the adapter

**Discover models without a first-run gate:** run `python3 <skill-dir>/scripts/prewalk.py models --navigator-host <your host>` (read-only). It lists currently usable Builder backends, models, and effort choices from the registry in `scripts/builder_backends/` ([references/backends.md](references/backends.md)). It never writes config or launches a test. Do not require `doctor`, `setup`, `test`, or an existing `~/.prewalk/config.json` before Prewalk; those are optional diagnostics and saved-preference tools. A ready saved config may supply a Builder default, but is never a prerequisite.

Adapter selection and capability validation happen before model/effort dispatch:

1. Read [adapters/README.md](adapters/README.md) and the selected `adapters/<host>.md`.
2. Identify the Navigator host (the current parent harness) and the Builder host (the configured executor). They may differ.
3. Validate the capabilities the run requires: initial launch, resumable session, session identity, final-message extraction, permission boundary, and native artifact completeness. If a required capability is missing, stop and report it; do not invent a flag, session id, usage value, or final message.
4. Record the Navigator host in `run.json` as `navigator_host` and the Builder adapter host id as `adapter_host`; pass the Builder adapter host id to the runtime adapter as `--host <host>`.

Each backend maps to a runtime host for `prewalk_runtime.py inspect --host`, and adapter files under `adapters/` document those hosts. Never use a Navigator-class model (`opus`, `best`, `fable`, Codex Sol/Astra) as Builder; the registry rejects them. For another harness, the host needs a thin shim that emits the normalized envelope in [adapters/README.md](adapters/README.md); copying `SKILL.md` into a directory is not proof of support. Do not infer support for a harness merely because it can execute a prompt, and do not claim an adapter is live-tested when only fixtures exist.

## Connection setup only when needed

Do not make connection setup a separate first-use phase. Start with model discovery and the model/effort preflight. If the selected backend cannot actually launch because authentication or endpoint configuration is missing, report that concrete error and offer the other available models or the backend's documented managed setup path. Never ask for credentials on every run, repeat a valid setup, or run connection setup during Builder execution, Navigator review, or resume from a phase marker.

When setup is genuinely needed, ask only for the missing information through the host's secure mechanism. Never echo credentials or put them in a handoff, progress file, log, or commit. Apply changes only through the adapter's documented managed configuration path; never hand-edit TOML, JSON, credentials, or runtime copies. Configuring a provider or endpoint is not proof that a requested model or effort works: keep the preflight and its validation below.

## Model and effort preflight

For an orchestrated Navigator run, ask the user to confirm the model pairing before **Orient**. Use the host's ordinary question mechanism; structured questions are optional, and plain text is sufficient. Reuse values already supplied in the invocation and ask only for missing values. Do not ask again during executor execution, review, or resume from a phase marker.

**Recommended defaults (Prewalk):** Navigator uses the current parent-session model and its current effective effort; Builder uses an available weak model with effort `medium`. Navigator decides; Builder follows the first-edit template — do not default Builder to `max`, because max mostly re-plans work that is already decided and burns tokens. Pure pattern expansion may use Builder `low`; repeated compile/test failures may raise Builder to `high`; escalate back to Navigator instead of jumping to Builder `max`.

Before asking, load the last confirmed pairing with `python3 <skill-dir>/scripts/prewalk_prefs.py show --json` (file: `~/.prewalk/last-pairing.json`, overridable by `PREWALK_PREFS_PATH`). If a last pairing exists, offer **reuse last: &lt;navigator model/effort → builder model/effort&gt;** as the first option. If the user accepts reuse or **use defaults**, skip the four questions.

Otherwise ask these four questions together, stating the recommended defaults in the prompt:

1. Which model should Navigator use? Default: the current parent-session model.
2. Which thinking effort should Navigator use? Default: the current parent session's effective effort, not a new global default.
3. Which model should the Builder executor use? Default: an available weak model from `prewalk.py models` (for example `sonnet`, `gpt-6-luna`, or `kimi-code/kimi-for-coding`).
4. Which thinking effort should the Builder executor use? Recommended default: `medium` (not `max`) when the selected model supports it. If `prewalk.py models` reports no effort control, state that effort is not settable and record `null`; do not claim `medium` was applied.

Accept **use defaults** as the answer to all four. One-liner to show the user: "Navigator keeps the current session model and effort; Builder medium follows the template — usually skip Builder max." If the host cannot ask questions, use the recommended defaults (or the last saved pairing when present) and report them; do not block an unattended run merely because the prompt is unavailable.

After the user confirms a pairing, persist it with `python3 <skill-dir>/scripts/prewalk_prefs.py save ...` (non-secret fields only: models and efforts). Next orchestrated run should offer that pairing for reuse.

A selection is not proof that it took effect. Before any implementation edit or dispatch, validate the requested values and record both requested and effective models and efforts in the handoff:

- Each effort must be supported by its selected model; never silently substitute a different value.
- If the requested Navigator model or effort is not the effective parent-session value, pause and ask the user to apply it through the host's native model/configuration mechanism, then rerun Prewalk. Do not claim that answering the prompt switched the parent session.
- If the requested Builder model or effort is unavailable through the selected adapter's managed surface, pause before dispatch. Do not hand-edit credentials or configuration, bypass the managed surface, or dispatch a different model while reporting the requested one. The user must choose the surface's actual pairing or switch to an adapter/surface that provides the requested model. Do not imply that arbitrary setup makes every model valid.
- For the Codex DeepSeek managed surface, the manager default Builder effort is `medium` (`--effort` / `CODEX_DEEPSEEK_EFFORT`); pass the validated Builder effort into the launch command rather than assuming `max`.
- Once validated, carry the effective values unchanged through execution, return, and review. A legacy handoff without these fields uses the values visible in its actual launch and parent session; do not re-ask.

## Shared rules

- Read applicable `AGENTS.md` files and inspect worktree status before acting.
- Preserve unrelated dirty-worktree changes and state the inherited baseline.
- Treat observed repository state, tests, logs, and diffs as authoritative over handoff prose.
- Never search the web or public issue history for an existing solution unless the user explicitly asks. Repository-local history and documentation are allowed.
- Report exact commands and outcomes. Do not claim checks that were not run.

## Builder executor contract

- Treat Builder as the execution role; "Builder boundary" describes the responsibility boundary, not a model name, vendor, or harness.
- Dispatch only through the selected adapter's documented launch and resume paths. Do not substitute an in-harness subagent unless that adapter explicitly documents support for it, and do not hand-maintain provider configuration, credentials, or runtime copies.
- Adapter configuration and routing verification belong to the adapter's managed surface. If its readiness status is not configured/ready, run the adapter's documented repair step before dispatching.
- Keep one resumable Builder session for one task, repository/worktree, branch, model/effort pairing, and permission boundary. Read [session-continuity.md](references/session-continuity.md) before dispatch and review. The initial turn is the full handoff; a supplement, escalation decision, or rework resumes the recorded session with a `PREWALK_PHASE: BUILDER_RESUME` delta. Keep stable role and execution instructions before variable run metadata; append deltas instead of rewriting earlier prompts.
- Give the executor a self-contained handoff package because it receives no parent context. Launch it from the task workspace with the adapter's documented writable permission boundary so it can edit the repository.
- If the Builder model cannot accept images, inspect visual inputs in Navigator and encode all necessary visual facts, paths, constraints, and acceptance criteria in the handoff. If it can accept images, still record the facts Navigator relied on.
- Delegate by judgment density, not superficial difficulty. Navigator keeps the plan, requirement and ambiguity interpretation, architecture and correctness-critical choices, and final review. Builder takes mechanical implementation even when it is technically hard — pattern-following refactors, exhaustive edits, build/test wiring, and debugging against an observed failure — because the pattern is settled and the remaining work is bounded execution rather than new intent decisions.
- Keep final judgment in Navigator. Builder implements, debugs, validates, and reports; it does not perform the final review.

## Bounded escalation

Escalate only when the work cannot proceed correctly without a decision that belongs to Navigator:

- A requirement is genuinely ambiguous or self-contradictory after the available evidence is exhausted, and the choice changes user-visible behavior.
- Correctness requires a new architectural, scope, or direction decision at lead level, not a choice inside the pattern the handoff already established.
- Two or more defensible options remain with materially different outcomes and the handoff does not authorize one of them.

Ordinary implementation difficulties, compile errors, failing tests, flaky environments, and unclear library behavior are executor work: diagnose them, try the documented approach, and report what was tried. Do not escalate at the first failed check, and do not guess a requirement to keep moving.

On escalation, stop the affected area, preserve the work already done, and return the standard **Builder to Navigator** package with state `escalated`. State the exact decision required, the evidence that forced it, the options considered, and the smallest safe default. Navigator makes that decision and, if work should continue, resumes the same recorded Builder session with a `PREWALK_PHASE: BUILDER_RESUME` delta (legacy `DEEPSEEK_RESUME` is accepted) carrying it (see Phase A); only explicit user authorization permits a cold-recovery full handoff. The executor never redispatches itself.

## Phase A: Navigator prewalk and orchestration

### Orient

Restate the requested outcome and constraints briefly. Use targeted search to trace the behavior from its entry point through relevant state, dependencies, and validation surfaces. Prefer evidence in this order:

1. Reproduction, failing test, log, or compiler error
2. Call sites and nearby tests
3. Implementations and configuration reached at runtime
4. Local documentation and version-control history

Record the leading explanation and important eliminated hypotheses. Avoid unrelated subsystems.

### Pass the confidence gate

Do not edit until all are known:

- The behavior or invariant that must change
- The responsible symbol, configuration, or boundary
- Why the intended edit addresses the cause
- A focused validation command or check
- Compatibility risks and behavior that must remain unchanged

If blocked, do not manufacture an edit. Return the missing evidence or authority and do not dispatch Builder.

### Create the trajectory

Create a TODO list using the task's plan or TODO mechanism:

- Use 3-8 items with exactly one in progress.
- Attach validation to every implementation item.
- Keep focused and broader regression verification as explicit items when appropriate.
- Make the first implementation item the smallest coherent slice that demonstrates the intended pattern.

### Classify and write the plan before starting Builder

Do not start any Builder process for exploration. Classify after Navigator has enough deterministic evidence:

- `brief_only`: mechanical remaining work with verification; no Navigator code edit.
- `sample_guided`: exactly one coherent difficult slice needs a Navigator L1 sample; extend it with Builder.
- `navigator_only`: judgment dominates or verification is unavailable; finish directly without Builder.

Read [builder-fusion.md](references/builder-fusion.md) and write `plan.json` with `prewalk_plan.py create`, including goal, scope, acceptance, and independent validation commands. `navigator_only` needs no Builder plan or launch. For `sample_guided`, capture L0 before editing, land exactly one coherent L1 sample, inspect its complete diff, and retain `first-edit.patch`. Do not debug onward or start a second implementation step in Navigator.

### Dispatch at the Builder boundary

For a persistent backend, allocate the run directory outside the source worktree and use the persistent protocol. `kimi-sub` uses ACP over stdio; other backends that lack a persistent driver must fail explicitly or use the documented legacy path with the non-persistent limitation stated.

```sh
python "<installed-prewalk>/scripts/prewalk_builder.py" launch --run-dir "<run-dir>" --plan "<run-dir>/plan.json" --backend kimi-sub
python "<installed-prewalk>/scripts/prewalk_builder.py" status --run-dir "<run-dir>"
python "<installed-prewalk>/scripts/prewalk_builder.py" execute --run-dir "<run-dir>" --text "<short execute delta>"
python "<installed-prewalk>/scripts/prewalk_builder.py" review --run-dir "<run-dir>"
```

Wait for `AWAITING_NAVIGATOR` before `execute`; this is a live wait in the same PID/session, not a new Builder process. The supervisor creates an isolated worktree based on L1 when present, otherwise L0. It retains full diffs, independently runs the plan's validation commands, and writes `builder-review.patch/json` plus `validation/` records. Do not read Builder's prose summary or treat it as evidence.

Plans, indexes, patches, and continuations use content hashes so identical content is not rebuilt or rewritten. Every prompt starts with the stable `PREWALK_PHASE: PERSISTENT_BUILDER` and `SESSION_CACHE_KEY` prefix; only phase instructions and deltas vary. Builder environments strip other providers' credentials and model overrides while preserving Desktop OAuth configuration. The supervisor records executable path/size/SHA256 and ACP `currentValue` model evidence, and rechecks integrity before execute/approve. Hash evidence proves the binary did not change during the run, not vendor authenticity. Usage is recorded per role when the host exposes it; missing usage is `unavailable` or `not_collected`, never zero or proof of model identity.

Navigator reviews the actual patch and validation evidence, then explicitly approves or discards. Approval runs `git apply --check` before applying `base..final` to the original worktree; nothing merges automatically. Failed worktrees remain for diagnosis until Navigator explicitly discards or salvages them.

```sh
python "<installed-prewalk>/scripts/prewalk_builder.py" approve --run-dir "<run-dir>"
# or, after a deliberate decision:
python "<installed-prewalk>/scripts/prewalk_builder.py" discard --run-dir "<run-dir>"
```

For corrections, send another short `execute` delta to the same live Builder. For model/effort changes, provide a real compaction id and use `route --compaction-id <id>` so the ACP session and PID stay intact and `routes.jsonl` records the reason. Do not switch models on the first failure or send keepalive prompts.

### Optional async mode

Serial blocking dispatch above is the default. Only when the user asks for it, or Navigator has independent work while Builder runs, follow [async-mode.md](references/async-mode.md) instead: before the first edit, capture the inherited worktree as L0 with `prewalk_layers.py snapshot --layer L0`; then make the first edit and launch with a short brief through `scripts/prewalk_async.py`. Keep working, read only the new progress tail with `status --after`, redirect with `steer`, stop with `cancel`. Final review rules are unchanged.

### Resume the Builder session

When continuing the same task and boundary — a supplement, an escalation decision, or rework — do not start another Builder session. Read [session-continuity.md](references/session-continuity.md), load `run.json`, and send only a `PREWALK_PHASE: BUILDER_RESUME` delta from [handoff-format.md](references/handoff-format.md). Legacy `PREWALK_PHASE: DEEPSEEK_RESUME` deltas are accepted for existing sessions. The delta repeats the unchanged `HANDOFF_ID` and expected Builder session id and carries only the new decision, new evidence or constraints, current TODO state, progress path, and the normal `NAVIGATOR_REVIEW` return requirement. Do not resend the original objective, repository inventory, old evidence, first edit, or full handoff.

Launch the resume from the original workspace with the selected adapter's documented resume command. Do not add model, effort, provider, or permission flags that were not part of the recorded launch; repeat only the grants the adapter documents as required on resume. Capture a new turn artifact and never overwrite earlier turns. Then run the runtime adapter with `--host "<adapter-host>" --builder-turn "<next-turn>" --expected-session "<builder-session-id>" --usage "<run-dir>/usage.json"`; for turn 2 and later the same cumulative usage file is required prior state. A changed session id, missing or incompatible prior usage, or a host-specific artifact disagreement is a failure, not a new baseline. Confirm the inherited workspace and model/effort boundary from the launch and usage evidence; a mismatch means the resume crossed a boundary and must be reported. A resume failure must not silently cold-start: preserve the work and report the routing/session error. Cold recovery requires explicit user authorization and a recorded reason, and starts a new Builder session. Start a different session for a different objective, repository/worktree, branch, model/effort/provider, or permission boundary. Prefer a model or effort change at an observed compaction or execution boundary where context must be rebuilt anyway, but never postpone a correctness-required change to protect cache. Do not invent forced-compaction commands or send keepalive prompts.

## Phase B: Builder execution

### Activate executor mode

Treat `PREWALK_PHASE: BUILDER_EXECUTE`, `PREWALK_PHASE: BUILDER_RESUME`, and the accepted legacy markers `PREWALK_PHASE: DEEPSEEK_EXECUTE`, `PREWALK_PHASE: DEEPSEEK_RESUME`, and `PREWALK_PHASE: LUNA_EXECUTE` as explicit cancellation of Navigator's stop boundary. Retain the incoming `HANDOFF_ID` unchanged for the return package. Do not invoke Navigator prewalk again. On the initial turn, inspect worktree status, the retained first diff, and the handed-off evidence before reopening files; on a resumed turn, inspect current worktree state and follow only the new delta within the existing context.

Adopt the handed-off TODO list. Re-plan only when repository evidence invalidates it; record the reason and keep the list bounded.

### Publish progress

Read [progress-format.md](references/progress-format.md) and the handoff's `PROGRESS CHANNEL` before inspecting files. Append the first event to the stated `progress.jsonl` before touching the repository, then keep appending one complete UTF-8 JSON line — flushed immediately — when a TODO starts, completes, or is replanned; before and after long builds and tests or a meaningful batch of edits; and when blocked, escalated, or complete. On a resumed turn, append to the same progress file and continue `seq`; set the optional `turn` field to the Builder turn number. Include the sequence number, state, short summary, current TODO, known changed paths, validation status, and next step. Never include secrets, environment files, tokens, hidden reasoning, or raw command output.

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

Read [handoff-format.md](references/handoff-format.md). Return a concise completion status followed by one fenced, copy-ready package using the **Builder to Navigator** template. Generate it whether execution completed, became blocked, or was escalated. The package begins with `PREWALK_PHASE: NAVIGATOR_REVIEW`; the legacy `PREWALK_PHASE: SOL_REVIEW` remains accepted during migration.

Use the runtime adapter's extracted final message as the turn's final assistant response. Do not hand-parse a native stream or substitute a draft message for it.

Include the original objective, inherited first edit, complete changed-path inventory, implementation decisions, exact validation results, unresolved risks, and the diff range or baseline Navigator should review. Carry the model/effort selection forward exactly as recorded in the handoff, note any deviation you observed, and do not assert your own model identity beyond those recorded values. Record which decisions Navigator retained and did not hand off, and if you escalated, the exact decision required with the evidence that forced it. Include the final progress status and last sequence, and do not perform the Navigator review yourself.

## Phase C: Navigator review

Enter this phase after the orchestrated Builder child returns or when the incoming prompt begins with `PREWALK_PHASE: NAVIGATOR_REVIEW` (legacy `PREWALK_PHASE: SOL_REVIEW` is accepted). This is a review-only phase unless the user explicitly asks for fixes. Do not read or rely on Builder's summary. For a persistent run, inspect `builder-review.patch`, `builder-review.json`, and `validation/`; require actual diff and passing independent validation. For a legacy async run, start from `review.md` built from Git layers, not Builder prose. Review for:

- Requirement coverage and behavioral correctness
- Root-cause alignment and architectural consistency
- Error paths, concurrency, security, compatibility, and data migration risks
- Test quality and gaps between claimed and observed validation
- Unrelated or accidental changes

Run targeted read-only checks or tests when useful. Report actionable findings by severity with precise paths and lines. If no findings exist, say so explicitly and list residual risks or checks not performed. Include a concise execution/validation status. For a persistent run, require `READY_FOR_REVIEW`, matching PID/session, actual diff, and validation artifacts before `approve`; no Builder text is required. For a legacy run, inspect `run.json` and that turn's `prewalk_runtime.py` output, including its return marker, session identity, and cumulative usage. Treat all usage and cost records as observability only, never proof of quality or model identity.

If a progress file exists, read it as navigation context only. A missing or stale progress file is not proof of failure or of a hung process: inspect the background task and raw output before diagnosing, and report the last event time rather than inventing activity. Never treat the final progress event as approval evidence — verify the actual diff.

An `escalated` or `blocked` return is reviewed the same way, with the preserved work and the evidence checked before the request is accepted. If the requested decision is genuinely Navigator's, make it explicitly — or take it to the user when it is a product, scope, or risk decision — and, if work should continue, resume the same recorded Builder session with a `PREWALK_PHASE: BUILDER_RESUME` delta (legacy `DEEPSEEK_RESUME` accepted) carrying that decision (see Phase A). Do not issue a fresh full handoff unless the user explicitly authorizes cold recovery or the task or boundary materially changed. Only a genuinely different task or boundary starts a new Builder session without calling it recovery. Do not self-redispatch or loop executor dispatches.

## Guardrails

- Do not create a standalone plan file as the Navigator deliverable.
- Do not omit the self-contained Builder prompt at the dispatch or recovery boundary.
- Do not let Navigator continue implementation while Builder is responsible for execution.
- Do not let Builder self-approve its implementation.
- Do not escalate ordinary implementation, compile, or test failures; diagnose them in the executor.
- Do not guess requirement, architecture, or scope decisions; return them to Navigator.
- Do not self-redispatch after an escalation return; Navigator decides first.
- Do not silently cold-start a new Builder session after a resume failure or an expired provider cache; cold recovery requires explicit user authorization and a recorded reason.
- Do not label model-call usage records as Builder turns; keep `calls` and `turns` distinct in the cumulative usage artifact.
- Do not send keepalive prompts or force compaction merely to chase cache hits; cache is an optimization, not a correctness goal.
- Do not expose secrets, full environment files, or irrelevant diffs in handoff prompts.
- Do not put credentials in a handoff, progress file, log, or commit; configure connections only through the adapter's managed path.
- Do not treat progress events as proof of completion or as a substitute for reviewing the actual diff.
- Do not invent host commands, flags, session ids, usage, cache hits, or costs; an unavailable capability stays unavailable.
- Do not claim all harnesses are automatically supported or all adapters live-tested.
- Do not claim end-to-end completion before Navigator has inspected the returned work and actual diff.
