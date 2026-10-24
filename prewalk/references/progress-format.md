# Builder progress channel

Use one run directory outside the repository, containing the handoff, `progress.jsonl`, stdout, and stderr. Give its absolute paths to Builder in the handoff. Progress files are operational records, not a substitute for the final return package or Navigator's diff review.

## Builder writes

Create `progress.jsonl` before inspecting the retained diff. Append one complete UTF-8 JSON object and newline per event, flushing each append. Only Builder writes this file; Navigator reads it. Do not buffer events until the run ends.

On a resumed Builder turn, append to the same `progress.jsonl`, continue increasing `seq`, and set the optional `turn` field to the Builder turn number. Never overwrite or replace an earlier turn's events; raw per-turn event files are separate artifacts and the progress file is cumulative.

Write an event:

- when execution starts;
- whenever a TODO starts, completes, or is replanned;
- before and after a long build/test command or a meaningful batch of edits;
- when blocked, escalated, or complete.

Each event contains:

```json
{"ts":"2026-10-24T12:00:00Z","handoff_id":"example","seq":1,"turn":1,"state":"started","summary":"Inspecting the retained first edit","current_todo":"Inspect baseline","changed_paths":[],"validation":null,"next":"Implement the agreed slice"}
```

`seq` increases from 1 and continues across resumed turns. `turn` is optional and carries the Builder turn number (one process invocation), not a model-call index; model-call counts belong in `usage.json`, not progress. Read `ts` from the actual clock for every event; never estimate timestamps. `state` is one of `started`, `step_started`, `step_completed`, `validation`, `heartbeat`, `blocked`, `escalated`, or `completed`. Keep `summary`, `current_todo`, and `next` short. `changed_paths` is the known inventory so far. `validation` is `null` or an object with a redacted `command`, `status` (`running`, `passed`, `failed`, or `not_run`), and concise `result`.

Record heartbeats at natural checkpoints; do not claim a timer or fixed reporting interval that the executor cannot guarantee. Do not include hidden reasoning, code dumps, credentials, environment files, tokens, or unredacted command output. Never put a secret in a command line or edit raw stdout/stderr afterward: raw captures must remain intact, and any suspected secret exposure must be reported for redaction/rotation. Progress is self-reported status until Navigator verifies the process and worktree.

## Navigator reads

After dispatch, report the run ID, background task ID when available, and where live status can be inspected. At natural checkpoints while the parent turn is active, take a non-blocking process/output snapshot and read only the new tail of `progress.jsonl`; do not busy-poll. Mirror the latest verified high-level state into the parent TODO or a concise progress note without replacing Navigator's own review items.

If the user asks for progress, read the latest progress event and process state before answering. Raw stdout/stderr remain available for tool-level detail. A missing or stale progress file is not proof of a hung process: inspect the background task and output before diagnosing it, and report the last event time rather than inventing activity.

If the parent turn ends, updates stop until a user message or task-completion notification starts another turn; the child may continue running. The host's background-task panel can show process state, but this file-based protocol does not create a native live subagent view in the parent chat. On completion, require the normal Builder return package and inspect the actual diff regardless of the final progress event.
