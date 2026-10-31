# Prewalk persistent Builder design

## 1. Roles and invariants

- **Navigator** classifies and plans before any Builder process starts. It owns ambiguity, architecture, the optional L1 sample edit, and final approval. It does not read Builder prose or watch the full execution transcript.
- **Builder** is one persistent process/session, never parallel and never best-of-N. It starts only after Navigator has produced a complete plan. It performs scoped execution checks, implementation, and validation, then returns machine evidence.
- Scripts provide deterministic Git/rg navigation. A repo map or LSP is optional. Serena and MCP language servers are alternative enhancements, never requirements or separate mandatory map formats.
- **Builder is not an approver.** Navigator accepts only actual diffs, test exit codes and necessary raw output, runtime/session/model evidence. Builder summaries, CHANGED/VALIDATION claims, and self-assessments are not acceptance evidence.

## 2. Navigator-only orientation

No Builder process or model call is allowed before classification and planning are complete.

1. Start with `git status/diff`, targeted ripgrep, existing tests, and at most a few relevant hunks.
2. If needed, generate one deterministic `exploration-index.json`. It contains paths, lines, symbols, relations, and one-sentence notes, not a code dump.
3. Navigator reads only a small `nav-digest.json`: file/symbol counts, dependency and risk flags, candidate L1, test entry points, and uncertainty. It never reads the full index merely to classify.
4. If the digest is insufficient, Navigator performs targeted reads. Do not start Builder to solve Navigator's classification problem.

Choose exactly one mode:

- `brief_only`: the remaining work is mechanical, substantial enough to delegate, and covered by tests. Navigator writes no code.
- `sample_guided`: one coherent difficult slice benefits from a Navigator sample. Navigator lands L1 and Builder extends the pattern.
- `navigator_only`: judgment dominates, the work cannot be verified, or delegation would cost more than doing it. No Builder starts.

Classification is approximate and uses remaining mechanical volume, the need for a sample, and verification coverage. New repository evidence can trigger reclassification, but Builder never changes the plan itself.

## 3. Shared artifacts, not shared transcripts

Run directory layout:

```text
plan.json                 # mode, goal, scope, acceptance, constraints, base refs
exploration-index.json    # optional full deterministic index; queried on demand
nav-digest.json           # small classification view only
brief.json                # stable, bounded execution brief
first-edit.patch          # complete L0..L1 patch when L1 exists
layers.json               # L0, optional L1, L2-n and errors
turn-NNN-events.jsonl     # native/host evidence, not Navigator reading material
turn-NNN.err
validation/               # commands, exit codes, stdout/stderr, timestamps
state.json                # persistent Builder state machine
progress.jsonl            # bounded machine events, read by seq only
routes.jsonl              # model/effort route reason and compaction evidence
usage.json                # per-role/model-call usage and cache evidence
builder-usage.json        # cumulative Kimi wire usage, when available
continuation-<id>.json    # compact routing state, never a transcript
```

The physical index is shared, but model reading is not duplicated: Navigator consumes only the digest; Builder queries the full index by symbol/hunk after launch. Plan prose can be path-light, but machine-checkable `scope` remains mandatory.

The complete diff is a first-class specification. It removes the need for a verbose plan. Do not replace it with prose and do not resend it on every resume. Plan, index, review patches, and continuations carry content fingerprints; identical content is not rebuilt or rewritten. Volatile timestamps are excluded from fingerprints.

## 4. Persistent Builder state machine

```text
PLANNED (Navigator only, no Builder)
  -> EXPLORING (Builder starts, scoped checks only)
  -> EXPLORATION_READY (phase artifact, nonterminal)
  -> AWAITING_NAVIGATOR (same PID/session waits for a command)
  -> EXECUTING (short execute delta with plan/L1 references)
  -> VALIDATING
  -> READY_FOR_REVIEW (terminal Builder phase, evidence complete)
  -> Navigator accepts, requests a correction, takes over, or discards
```

`EXPLORATION_READY` is not `NAVIGATOR_REVIEW`; no final response, session teardown, or cold start is allowed. `AWAITING_NAVIGATOR` requires an interactive channel (ACP/stdio or a versioned command queue), not just a prompt telling Builder to wait.

A one-shot CLI resumed with `--session` preserves history but is not process persistence. Such an adapter must not claim the persistent contract. Unsupported hosts fail explicitly or use the legacy serial path with that limitation stated.

The execute delta contains only the command, plan/L1 references, and any new constraints. The same PID/session and worktree must be used. If execution checks contradict the plan, Builder pauses for Navigator instead of changing scope or plan. Every prompt shares the same `PREWALK_PHASE: PERSISTENT_BUILDER` and `SESSION_CACHE_KEY: prewalk/<handoff-id>` prefix; only the trailing phase instructions and deltas change.

## 5. Pure-Git isolation and checkpoints

No LayerFS installation is required. Git objects and an isolated Builder worktree provide the sandbox.

- **L0** captures the inherited worktree before Navigator's first edit, including pre-existing dirty changes. It never modifies the real HEAD, branch, index, or worktree.
- **L1** is optional. `brief_only` starts from L0; `sample_guided` starts from L1. The complete L0..L1 patch is retained.
- Builder starts in a separate worktree based on L1 when present, otherwise L0. Navigator and Builder never edit the same worktree concurrently.
- **L2-<n>** captures each progress batch and the final state. Batches may contain multiple steps; correctness is judged by complete captured changes, not layer count.
- Failed layers remain available for diagnosis until Navigator chooses to discard, salvage, or take over. There is no automatic deletion.
- The review base is L1 when present, otherwise L0. Approval produces a `base..final` patch. That patch is applied to the original worktree only after explicit Navigator approval and a conflict check. Builder output is never merged automatically.

## 6. Validation and acceptance

Builder returns process metadata, complete diffs, and validation artifacts. A valid validation record includes command, cwd, exit code, start/end timestamps, and captured stdout/stderr paths. Raw logs stay on disk; Navigator reads exit codes and only the necessary failure excerpts.

Navigator does not read Builder's summary. A return envelope may verify `HANDOFF_ID`, session identity, and phase integrity, but its prose has no authority. No diff or no verification means no approval, regardless of Builder's claims.

`prewalk doctor` may check configured and requested models, but actual model acceptance requires launch/event evidence. Backends without observed-model evidence report `unverified`, never a false `model_ok`. ACP `configOptions.currentValue` is recorded separately from usage-reported models; mismatches become warnings rather than silent fallbacks.

Builder environments remove other providers' credentials, base URLs, and model overrides while preserving the host's OAuth configuration. The executable path, size, and SHA256 are recorded and checked again before execute and approve. This detects changes during the run; it does not claim vendor-signature verification.

## 7. Cache-efficient routing

Ordinary compile/test failures stay with the same Builder. Do not raise effort on the first failure.

Evidence for routing includes repeated equivalent failures, unsupported scope/template edits, missing requirement authority, and context pressure. Mechanical work may move to a stronger Builder model at a natural compaction point. Requirement, architecture, or L1-direction decisions return to Navigator.

A compaction continuation contains only goal, constraints, git refs/diffs, remaining TODO, failed commands, retained decisions, and the permission boundary. It never contains the full transcript. Model/effort changes follow the host's documented session boundary and are recorded in `routes.jsonl`; hidden cold starts are forbidden.

Process persistence does not override provider cache TTL. No synthetic keepalive prompts. Stable brief prefixes and append-only deltas preserve whatever caching the provider supports.

## 8. Cost and quality measurements

Record separately for Navigator and Builder:

- input/output/cache-read/cache-creation tokens;
- cold starts, resumes, compaction and route events;
- plan, digest, brief, diff and execute-delta sizes;
- validation outcomes, rework count, and final review findings.

The primary cost metric is tokens per accepted task, not model unit price. A cheaper route is valid only when correctness and final review quality hold. Missing host usage is `unavailable` or `not_collected`, never zero.

## 9. Required acceptance tests

- No Builder process exists before plan completion.
- `brief_only` works without L1; `sample_guided` includes the complete L1 patch.
- Exploration does not terminate Builder; execute reuses the same PID/session with no repeated exploration payload.
- Navigator and Builder never edit concurrently; the original HEAD/index/worktree stay untouched until approval.
- Full L1/L2 diffs survive normal completion, crash, and batched snapshots.
- Failed layers are preserved until explicit disposal; approved patches are conflict-checked before application.
- Navigator acceptance requires actual diff and validation evidence, not Builder prose.
- Route/compaction events preserve the documented session boundary and are observable.
- Identical plan/index/patch/continuation content is not rewritten; stable prompt prefixes and cache keys hold across phases.
- Builder environment strips foreign credentials/model overrides without removing OAuth configuration, and executable/model/usage evidence is never invented.
