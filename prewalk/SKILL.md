---
name: prewalk
description: "Run an orchestrated coding handoff: SOL investigates and lands one credible edit, dispatches a configured native DeepSeek subagent to complete mechanical implementation, builds/tests, and debugging at the Luna execution boundary, waits for it, then SOL reviews the actual final diff. Also resume execution or review from PREWALK_PHASE markers, including an escalated executor return that asks SOL for a decision. Use when the user asks for Prewalk, SOL-to-DeepSeek execution, a DeepSeek executor with SOL review, frontier-model preparation before delegated coding, execution from a Prewalk package, or final review of returned executor work."
---

# Prewalk

Transfer an evidence-backed coding trajectory from SOL through a native DeepSeek executor and back to SOL review. Do not substitute a prose plan for contact with the code.

## Select the run mode

Choose the mode from the incoming request:

- **Orchestrated SOL run:** Use when no phase marker exists. Run SOL prewalk, dispatch DeepSeek, wait, then perform SOL review in the same parent task.
- **DeepSeek execution:** Use when the prompt begins with `PREWALK_PHASE: DEEPSEEK_EXECUTE` or the legacy marker `PREWALK_PHASE: LUNA_EXECUTE`. Complete the executor phase and return a SOL review package.
- **SOL review:** Use when the incoming prompt begins with `PREWALK_PHASE: SOL_REVIEW`.

Never apply the stop-after-one-edit boundary to DeepSeek execution. Never repeat SOL investigation in executor mode unless retained evidence is contradicted by the repository.

## Model and effort preflight

For an orchestrated SOL run, ask the user to confirm the model pairing before **Orient**. Use the host's ordinary question mechanism; structured questions are optional, and plain text is sufficient. Reuse values already supplied in the invocation and ask only for missing values. Do not ask again during executor execution, review, or resume from a phase marker.

Ask these four questions together:

1. Which model should SOL use? Default: the current parent model.
2. Which thinking effort should SOL use? Default: that model's configured effort.
3. Which model should the executor use? Default: the managed executor model.
4. Which thinking effort should the executor use? Default: that executor model's configured effort.

Accept **use defaults** as the answer to all four. If the host cannot ask questions, use the current managed defaults and report them; do not block an unattended run merely because the prompt is unavailable.

A selection is not proof that it took effect. Before any implementation edit or dispatch, validate the requested values and record both requested and effective models and efforts in the handoff:

- Each effort must be supported by its selected model; never silently substitute a different value.
- If the requested SOL model or effort is not the effective parent-session value, pause and ask the user to apply it through the host's native model/configuration mechanism, then rerun Prewalk. Do not claim that answering the prompt switched the parent session.
- If the requested executor model or effort is unavailable through the managed executor surface, pause before dispatch. Do not hand-edit credentials or configuration, bypass the managed surface, or dispatch the default while reporting the requested model. A fixed managed-executor contract is not broadened by this preflight: the user must choose the surface's default pairing or switch to a managed Prewalk variant/surface that provides the requested model. Do not imply that arbitrary setup makes every model valid.
- Once validated, carry the effective values unchanged through execution, return, and review. A legacy handoff without these fields uses the values visible in its actual launch and parent session; do not re-ask.

## Shared rules

- Read applicable `AGENTS.md` files and inspect worktree status before acting.
- Preserve unrelated dirty-worktree changes and state the inherited baseline.
- Treat observed repository state, tests, logs, and diffs as authoritative over handoff prose.
- Never search the web or public issue history for an existing solution unless the user explicitly asks. Repository-local history and documentation are allowed.
- Report exact commands and outcomes. Do not claim checks that were not run.

## DeepSeek executor contract

- Treat DeepSeek as the Luna execution role; "Luna boundary" describes the responsibility boundary, not a required model name.
- Dispatch only through the managed DeepSeek executor surface: a `codex exec` child process with `model=deepseek-flash`, `model_provider="deepseek"`, `model_reasoning_effort="max"`. Do not dispatch with the native `spawn_agent` tool: on desktop builds ≥ 0.146 the collaboration namespace is gated to the ChatGPT backend, so a native spawn cannot route a child to the DeepSeek provider and fails with a model-unsupported 400 (see openai/codex#31882, openai/codex#36957).
- Run the executor via the staged desktop runtime managed by the separate `codex-deepseek-subagent` skill (its `status`/`test`/`repair` commands own the runtime path, provider config, catalog, and credential). Do not hand-maintain TOML, JSON, credentials, or runtime copies for this contract.
- Configuration and routing verification belong to the `codex-deepseek-subagent` skill; if its status is not `configured`/`ready`, run its `repair` before dispatching.
- Give the executor a self-contained text prompt (the handoff package) because it receives no parent context. Launch it from the task workspace with a writable sandbox so it can edit the repository, and set `CODEX_HOME` to the Codex home that holds the managed configuration.
- DeepSeek is text-only. Inspect visual inputs in SOL and encode all necessary visual facts, paths, constraints, and acceptance criteria in the handoff.
- Delegate by judgment density, not superficial difficulty. SOL keeps the plan, requirement and ambiguity interpretation, architecture and correctness-critical choices, and final review. DeepSeek takes mechanical implementation even when it is technically hard — pattern-following refactors, exhaustive edits, build/test wiring, and debugging against an observed failure — because the pattern is settled and the remaining work is bounded execution rather than new intent decisions.
- Keep final judgment in SOL. DeepSeek implements, debugs, validates, and reports; it does not perform the final review.

## Bounded escalation

Escalate only when the work cannot proceed correctly without a decision that belongs to SOL:

- A requirement is genuinely ambiguous or self-contradictory after the available evidence is exhausted, and the choice changes user-visible behavior.
- Correctness requires a new architectural, scope, or direction decision at lead level, not a choice inside the pattern the handoff already established.
- Two or more defensible options remain with materially different outcomes and the handoff does not authorize one of them.

Ordinary implementation difficulties, compile errors, failing tests, flaky environments, and unclear library behavior are executor work: diagnose them, try the documented approach, and report what was tried. Do not escalate at the first failed check, and do not guess a requirement to keep moving.

On escalation, stop the affected area, preserve the work already done, and return the standard **DeepSeek to SOL** package with state `escalated`. State the exact decision required, the evidence that forced it, the options considered, and the smallest safe default. SOL makes that decision and, if work should continue, issues one fresh handoff carrying it; the executor never redispatches itself.

## Phase A: SOL prewalk and orchestration

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

If blocked, do not manufacture an edit. Return the missing evidence or authority and do not dispatch DeepSeek.

### Create the trajectory

Create a TODO list using the task's plan or TODO mechanism:

- Use 3-8 items with exactly one in progress.
- Attach validation to every implementation item.
- Keep focused and broader regression verification as explicit items when appropriate.
- Make the first implementation item the smallest coherent slice that demonstrates the intended pattern.

### Land the first credible edit

Make exactly one coherent implementation edit. It may include inseparable adjacent hunks or files, but must represent one atomic slice. Inspect its diff. Run only a fast syntax, compile, or focused check when needed to prove the edit is not malformed. Do not debug onward or start the second implementation step in SOL.

### Dispatch DeepSeek at the Luna boundary

Read [handoff-format.md](references/handoff-format.md) and populate the **SOL to DeepSeek** template entirely from observed evidence. Include the requested and verified model/effort selection, current TODO statuses, the first edit, the decisions SOL retained, inherited worktree conditions, commands, failures, risks, visual facts when applicable, and the next validation.

When the executor surface is ready (per the `codex-deepseek-subagent` skill status), create a short unique `HANDOFF_ID`, write the populated handoff package to a temporary file, then launch the managed executor with the exec tool using this shape (adjust quoting for the active shell; `<` redirects the package into stdin):

```sh
CODEX_HOME="$CODEX_HOME" "<staged-codex.exe>" exec --skip-git-repo-check -s workspace-write -C "<task workspace>" -m deepseek-flash -c model_provider="\"deepseek\"" -c model_reasoning_effort="\"max\"" - < "<handoff-package-file>"
```

where `<staged-codex.exe>` is the desktop runtime path reported by the `codex-deepseek-subagent` skill (run its `status`). Keep `-s workspace-write`: sandboxed execution is part of this contract and works on paths with spaces as long as the staged runtime bundle is complete (`codex.exe`, `codex-code-mode-host.exe`, `codex-windows-sandbox-setup.exe`, `codex-command-runner.exe` — the last one is required for any sandboxed command to spawn; a staged copy missing it fails every command with `CreateProcessWithLogonW failed: 2`). Do not switch to `danger-full-access` or otherwise broaden permissions on your own initiative, and do not silently downgrade the sandbox either: if you believe the invocation needs a change, ask the user once. Allow a long timeout or run it as a background task for substantial packages. Do not implement in parallel or ask the user to copy the prompt.

After the executor process finishes, require its final message to contain `PREWALK_PHASE: SOL_REVIEW` and the same `HANDOFF_ID`. If the process failed before producing a turn (nonzero exit, auth or provider error, empty output), treat the result as a routing failure: do not claim execution or enter review, preserve the first edit, return the original copy-ready handoff, and run the `codex-deepseek-subagent` skill's `repair`. If repository evidence shows executor work occurred but only the return package is malformed, reconstruct the changed-path and validation inventory and proceed to Phase C. In all successful cases, inspect the actual worktree and treat the package as navigation context, not proof.

## Phase B: DeepSeek execution

### Activate executor mode

Treat `PREWALK_PHASE: DEEPSEEK_EXECUTE` and legacy `PREWALK_PHASE: LUNA_EXECUTE` as explicit cancellation of SOL's stop boundary. Retain the incoming `HANDOFF_ID` unchanged for the return package. Do not invoke SOL prewalk again. Inspect worktree status, the retained first diff, and the handed-off evidence before reopening files.

Adopt the handed-off TODO list. Re-plan only when repository evidence invalidates it; record the reason and keep the list bounded.

### Execute and verify

Complete the remaining implementation, debugging, focused tests, and appropriate regression checks. Preserve the first edit when sound; revise it when validation shows it is wrong. Track all changed paths and commands. Do not stop merely because the handoff's first suggestion fails. The mechanical work is yours even when it is hard: compile errors, failing or flaky tests, and unclear library behavior are problems to diagnose, not reasons to escalate.

Before finishing:

- Inspect the complete diff against the inherited baseline.
- Check requirements, edge cases, compatibility, and unintended changes.
- Run the strongest practical focused and regression validation.
- Mark TODO items accurately; leave unresolved work explicit.

### Return to SOL

Read [handoff-format.md](references/handoff-format.md). Return a concise completion status followed by one fenced, copy-ready package using the **DeepSeek to SOL** template. Generate it whether execution completed, became blocked, or was escalated.

Include the original objective, inherited first edit, complete changed-path inventory, implementation decisions, exact validation results, unresolved risks, and the diff range or baseline SOL should review. Carry the model/effort selection forward exactly as recorded in the handoff, note any deviation you observed, and do not assert your own model identity beyond those recorded values. Record which decisions SOL retained and did not hand off, and if you escalated, the exact decision required with the evidence that forced it. Do not perform the SOL review yourself.

## Phase C: SOL review

Enter this phase after the orchestrated DeepSeek child returns or when the incoming prompt begins with `PREWALK_PHASE: SOL_REVIEW`. This is a review-only phase unless the user explicitly asks for fixes. Inspect the actual worktree and diff; do not trust the executor summary alone. Review for:

- Requirement coverage and behavioral correctness
- Root-cause alignment and architectural consistency
- Error paths, concurrency, security, compatibility, and data migration risks
- Test quality and gaps between claimed and observed validation
- Unrelated or accidental changes

Run targeted read-only checks or tests when useful. Report actionable findings by severity with precise paths and lines. If no findings exist, say so explicitly and list residual risks or checks not performed. Include a concise execution/validation status. Check the recorded model/effort selection against the actual launch command and the parent session; do not treat the executor's self-report as evidence of the model it ran.

An `escalated` or `blocked` return is reviewed the same way, with the preserved work and the evidence checked before the request is accepted. If the requested decision is genuinely SOL's, make it explicitly — or take it to the user when it is a product, scope, or risk decision — and, if work should continue, issue one fresh handoff carrying that decision. Except for carrying an escalation decision forward, do not regenerate another executor prompt unless the user requests remediation, and never loop executor dispatches.

## Guardrails

- Do not create a standalone plan file as the SOL deliverable.
- Do not omit the self-contained DeepSeek prompt at the dispatch or recovery boundary.
- Do not let SOL continue implementation while DeepSeek is responsible for execution.
- Do not let DeepSeek self-approve its implementation.
- Do not escalate ordinary implementation, compile, or test failures; diagnose them in the executor.
- Do not guess requirement, architecture, or scope decisions; return them to SOL.
- Do not self-redispatch after an escalation return; SOL decides first.
- Do not expose secrets, full environment files, or irrelevant diffs in handoff prompts.
- Do not claim end-to-end completion before SOL has inspected the returned work and actual diff.
