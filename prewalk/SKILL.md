---
name: prewalk
description: "Run an orchestrated Prewalk coding handoff: Navigator (the parent agent) investigates and plans, a configured Builder executor implements in one persistent session on an isolated worktree, and Navigator reviews the actual diff before approving. Use for Prewalk, Navigator-to-Builder execution, or to resume from PREWALK_PHASE markers (BUILDER_EXECUTE, BUILDER_RESUME, NAVIGATOR_REVIEW, or accepted legacy markers)."
---

# Prewalk

Transfer an evidence-backed coding trajectory from Navigator through a configured Builder executor and back to Navigator review. Do not substitute a prose plan for contact with the code. The persistent `kimi-sub` path needs only this file and [adapters/kimi.md](adapters/kimi.md); the legacy one-shot protocol lives in [references/legacy-oneshot.md](references/legacy-oneshot.md).

## Roles and harness independence

- **Navigator** is the parent agent. It keeps the plan, requirement and ambiguity interpretation, architecture and correctness-critical choices, and final review. It runs on whatever harness and model the user selected for the parent session.
- **Builder** is the execution role: it implements, debugs, validates, and reports; it does not perform the final review. A host adapter determines how Builder is launched, resumed, and observed.
- Core orchestration is harness-neutral: Navigator and Builder may use different harnesses, model vendors, and models. The shared contract is the handoff package, phase markers, progress channel, and runtime-extracted session/usage artifacts — not a particular vendor command.

## Select the run mode

- **Orchestrated Navigator run:** Use when no phase marker exists. Run Navigator prewalk, dispatch Builder, wait, then perform Navigator review in the same parent task.
- **Builder execution:** Use when the prompt begins with `PREWALK_PHASE: BUILDER_EXECUTE`. Legacy `PREWALK_PHASE: DEEPSEEK_EXECUTE` and `PREWALK_PHASE: LUNA_EXECUTE` identify the same phase and remain accepted.
- **Builder resume:** Use when the prompt begins with `PREWALK_PHASE: BUILDER_RESUME`. Legacy `PREWALK_PHASE: DEEPSEEK_RESUME` remains accepted. Continue the recorded Builder session; do not rerun Navigator prewalk or start a new session.
- **Navigator review:** Use when the incoming prompt begins with `PREWALK_PHASE: NAVIGATOR_REVIEW`. Legacy `PREWALK_PHASE: SOL_REVIEW` remains accepted.

Legacy markers exist for sessions and manager scripts that predate the neutral names; do not rename them outside this skill. Their executor-side and one-shot resume/review rules live in [references/legacy-oneshot.md](references/legacy-oneshot.md). Never apply the stop-after-one-edit boundary to Builder execution, and never repeat Navigator investigation in executor mode unless retained evidence is contradicted by the repository.

## Model and effort preflight

Discover models without a first-run gate: `python3 <skill-dir>/scripts/prewalk.py models --navigator-host <your host>` (read-only; registry in `scripts/builder_backends/`, see [references/backends.md](references/backends.md)). Adapter selection and capability rules are in [adapters/README.md](adapters/README.md) and [references/legacy-oneshot.md](references/legacy-oneshot.md); never use a Navigator-class model as Builder. If a backend cannot launch for missing auth or endpoint configuration, report the concrete error and offer the documented managed setup path — never a separate first-use setup phase, never credentials in a handoff, log, or commit.

For an orchestrated Navigator run, confirm the model pairing before **Orient**. Reuse supplied values; ask only for missing ones; do not ask again during execution, review, or resume.

**Recommended defaults (Prewalk):** Navigator uses the current parent-session model and its current effective effort; Builder uses an available weak model with effort `medium`. Navigator decides; Builder follows the first-edit template — do not default Builder to `max`, because max mostly re-plans work that is already decided and burns tokens. Pure pattern expansion may use `low`; repeated failures may raise Builder to `high`; escalate back to Navigator instead of `max`.

Load the last pairing with `python3 <skill-dir>/scripts/prewalk_prefs.py show --json` (`~/.prewalk/last-pairing.json`, overridable by `PREWALK_PREFS_PATH`). If one exists, offer **reuse last: &lt;navigator model/effort → builder model/effort&gt;** first. If the user accepts reuse or **use defaults**, skip the four questions below:

1. Navigator model? Default: the current parent-session model.
2. Navigator thinking effort? Default: the current parent session's effective effort, not a new global default.
3. Builder model? Default: an available weak model from `prewalk.py models`.
4. Builder thinking effort? Recommended: `medium` (not `max`) when the model supports it; if `prewalk.py models` reports no effort control, record `null` and do not claim `medium` was applied.

Persist a confirmed pairing with `prewalk_prefs.py save ...` (models and efforts only). A selection is not proof it took effect: each effort must be supported by its model; if the requested Builder pairing is unavailable through the managed surface, pause before dispatch rather than substituting silently; carry the effective values unchanged through execution, return, and review.

## Shared rules

- Read applicable `AGENTS.md` files and inspect worktree status before acting.
- Preserve unrelated dirty-worktree changes and state the inherited baseline.
- Treat observed repository state, tests, logs, and diffs as authoritative over handoff prose.
- Never search the web or public issue history for an existing solution unless the user explicitly asks. Repository-local history and documentation are allowed.
- Report exact commands and outcomes. Do not claim checks that were not run.

## Builder executor contract

- Dispatch only through the adapter's documented launch and resume paths; do not hand-maintain provider configuration, credentials, or runtime copies.
- Keep one resumable Builder session for one task, repository/worktree, branch, model/effort pairing, and permission boundary. The protocol design is in [builder-fusion.md](references/builder-fusion.md) and legacy session rules in [session-continuity.md](references/session-continuity.md); read them only when changing the protocol or using a legacy adapter.
- Give the executor a self-contained handoff; it receives no parent context. If the Builder model cannot accept images, encode all necessary visual facts in the handoff.
- Delegate by judgment density: Navigator keeps decisions; Builder takes mechanical implementation even when technically hard. Builder never performs the final review.

## Bounded escalation

Escalate only when work cannot proceed without a Navigator-level decision: a genuinely ambiguous or self-contradictory requirement that changes user-visible behavior, a new architectural/scope/direction decision, or defensible options the handoff does not authorize. Ordinary implementation difficulties, compile errors, failing tests, and flaky environments are executor work — diagnose them, try the documented approach, and report.

On escalation, stop the affected area, preserve the work, and return the **Builder to Navigator** package with state `escalated` and the exact decision required. Navigator decides and resumes the same session with a `BUILDER_RESUME` delta carrying it. The executor never redispatches itself.

## Phase A: Navigator prewalk and orchestration

### Orient

Restate the requested outcome and constraints briefly. Trace the behavior from its entry point through state, dependencies, and validation surfaces, preferring reproduction/failing-test evidence, then call sites and tests, then runtime implementations, then local docs and history. For a larger scope, run `prewalk_explore.py` to build one deterministic `exploration-index.json` plus a small `nav-digest.json` and read **only the digest** for classification; query the index later with `prewalk_explore.py query --symbol <name>` when needed. Serena and MCP/LSP servers are optional host tools, never requirements. Record the leading explanation and eliminated hypotheses.

### Pass the confidence gate

Do not edit until all are known: the behavior or invariant that must change; the responsible symbol, configuration, or boundary; why the intended edit addresses the cause; a focused validation command; and compatibility risks. If blocked, return the missing evidence or authority and do not dispatch Builder.

### Create the trajectory

Create a TODO list with 3-8 items, exactly one in progress, validation on every implementation item, and the smallest coherent first slice. For `sample_guided`, capture L0 before editing, land exactly one coherent L1 sample, inspect its complete diff, and retain `first-edit.patch`.

### Classify and write the plan before starting Builder

Do not start any Builder process for exploration. Classify after Navigator has enough deterministic evidence: `brief_only` (mechanical, verifiable; no Navigator edit), `sample_guided` (one Navigator L1 sample), or `navigator_only` (judgment dominates; finish directly). Write `plan.json` with `prewalk_plan.py create`; pass `--explore` only when Builder should run a scoped exploration turn first.

### Dispatch at the Builder boundary (persistent protocol)

Allocate the run directory outside the source worktree. `kimi-sub` uses ACP over stdio; backends without a persistent driver must fail explicitly or use the documented legacy path ([legacy-oneshot.md](references/legacy-oneshot.md)).

```sh
python "<installed-prewalk>/scripts/prewalk_builder.py" launch --run-dir "<run-dir>" --plan "<run-dir>/plan.json" --backend kimi-sub
python "<installed-prewalk>/scripts/prewalk_builder.py" execute --run-dir "<run-dir>" --text "<short execute delta>" --wait
# inspect builder-review.patch, builder-review.json, validation/
python "<installed-prewalk>/scripts/prewalk_builder.py" approve --run-dir "<run-dir>" --wait
```

With `explore` off (the default), launch goes straight to `AWAITING_NAVIGATOR`; the first `execute` prompt carries the full plan and later prompts are deltas only. With `explore` on, the exploration reply is stored in `turn-001-reply.md` and `CONTRADICTION:` lines land in `state.exploration_contradictions`.

`status` prints a compact summary: status, `idle`, `terminal`, `execution_count`, `handled_command_seq`, validation records, `has_diff`, `changed_files` (count), `changed_paths` (first 10), model status/warning, route recommendation, error, and the `savings` block; `status --full` dumps the complete state. `--wait` (default 3000 s, `--timeout` to change) is available on `execute`, `approve`, `discard`, and `cancel`; if it returns `"idle": false`, re-run it or inspect with `status`. A reply ending in `NOTE:` surfaces as `builder_note` only on `VALIDATION_FAILED`/`failed`. `cancel --wait` ends the run as `cancelled` and preserves the worktree.

The supervisor creates an isolated worktree from L1 when present, otherwise L0; retains full diffs; runs the plan's validation commands independently; and writes `builder-review.patch/json` plus `validation/` records. Do not read Builder's prose summary or treat it as evidence. Builder environments strip foreign credentials and model overrides while preserving Desktop OAuth; executable SHA256 and ACP `currentValue` model evidence are rechecked before execute/approve — this detects changes during the run, not vendor authenticity.

For corrections, send another short `execute --wait` delta to the same live Builder. For model/effort changes, provide a real compaction id and use `route --compaction-id <id>` so the ACP session and PID stay intact and `routes.jsonl` records the reason. Do not switch models on the first failure or send keepalive prompts.

A supplement, escalation decision, or rework continues the same live session with another `execute` delta — never a new session. The legacy one-shot resume protocol (`--session`, `prewalk_runtime.py inspect`) is in [references/legacy-oneshot.md](references/legacy-oneshot.md).

## Phase B: Builder execution

Persistent Builders run inside the supervisor and return machine evidence, not a prose package. The Builder-side executor rules (activation, progress channel, verification, return package) apply to legacy one-shot adapters and live in [references/legacy-oneshot.md](references/legacy-oneshot.md).

## Phase C: Navigator review

Enter after the persistent run reaches `READY_FOR_REVIEW` or when the prompt begins with `PREWALK_PHASE: NAVIGATOR_REVIEW` (legacy `SOL_REVIEW` accepted). Review-only unless the user asks for fixes. Do not rely on Builder's summary: inspect `builder-review.patch`, `builder-review.json`, and `validation/`; require an actual diff, matching PID/session, and passing independent validation before `approve`. Legacy runs follow the checks in [legacy-oneshot.md](references/legacy-oneshot.md).

Review for requirement coverage and behavioral correctness; root-cause alignment; error paths, concurrency, security, compatibility; test quality and gaps between claimed and observed validation; and unrelated changes. Report findings by severity with paths and lines; if none, say so and list residual risks. Usage and cost records are observability only; a progress file is context, never approval evidence. An `escalated`/`blocked` return is reviewed the same way: decide explicitly if it is genuinely Navigator's and resume the same session with a `BUILDER_RESUME` delta carrying the decision.

## Per-run savings report

On every terminal status the supervisor writes `savings.json`/`savings.md` and appends to `~/.prewalk/savings-history.jsonl`: an estimate of cost saved versus the Navigator doing all the work, priced from a user-maintained `prices.json` (see README). On Kimi the Navigator session is discovered from its wire records automatically; on other hosts run `record-usage` (tokens, or `--model` for just the model) before approving. Missing prices or an unknown Navigator model produce token counts and `unpriced` — never a fabricated percentage; Builder-only figures are an upper bound, and the report states its assumptions.

## Guardrails

- Do not create a standalone plan file as the Navigator deliverable.
- Do not omit the self-contained Builder prompt at the dispatch or recovery boundary.
- Do not let Navigator continue implementation while Builder is responsible for execution.
- Do not let Builder self-approve its implementation.
- Do not escalate ordinary implementation, compile, or test failures; diagnose them in the executor.
- Do not guess requirement, architecture, or scope decisions; return them to Navigator.
- Do not self-redispatch after an escalation return; Navigator decides first.
- Do not silently cold-start a new Builder session after a resume failure; cold recovery requires explicit user authorization and a recorded reason.
- Do not label model-call usage records as Builder turns; keep `calls` and `turns` distinct in the cumulative usage artifact.
- Do not send keepalive prompts or force compaction merely to chase cache hits; cache is an optimization, not a correctness goal.
- Do not expose secrets, full environment files, or irrelevant diffs in handoff prompts.
- Do not put credentials in a handoff, progress file, log, or commit; configure connections only through the adapter's managed path.
- Do not treat progress events as proof of completion or as a substitute for reviewing the actual diff.
- Do not invent host commands, flags, session ids, usage, cache hits, or costs; an unavailable capability stays unavailable.
- Do not claim all harnesses are automatically supported or all adapters live-tested.
- Do not claim end-to-end completion before Navigator has inspected the returned work and actual diff.
