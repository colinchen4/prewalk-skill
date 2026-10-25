# DeepSeek Harness (DSH) adapter

Host id: `dsh`. Use `prewalk_runtime.py inspect --host dsh`.

DeepSeek Harness is a plugin-based coding harness. This adapter is a fixture-tested binding, not a claim that DSH auto-discovers Agent Skills or that every DSH profile supports Prewalk.

## Load the core instructions

DSH does not automatically discover this skill as an Agent Skill. Load the Prewalk core through the native profile/plugin/prompt mechanism the operator has installed. Do not invent a plugin manifest, a profile name, or an instruction-discovery convention. If the installed profile cannot load the core instructions, report the missing capability and stop; copying `SKILL.md` into a directory is not proof of support.

The handoff package is still self-contained. Pass it through the headless stdin input: a lone `-`, or an omitted task argument, reads the prompt from stdin.

## Initial dispatch

The observed headless projection opens with `type=session` carrying `sessionId` and closes with `type=final` carrying `sessionId`, a durable reason, and the final answer.

```sh
cd "<task workspace>" && dsh --profile headless --json - < "<run-dir>/turn-001-handoff.txt" > "<run-dir>/turn-001-events.jsonl" 2> "<run-dir>/turn-001.err"
```

The lone `-` is the official stdin prompt input; do not invent a prompt flag or a JSON field. The `--profile headless --json` projection is not the complete session log: non-final events may be truncated, so do not reconstruct prompts, reasoning, or usage from the projection. Both process success and `final.reason.kind == "completed"` remain required; process exit zero alone is not sufficient.

Permissions, approvals, and extra-directory grants are explicit. Determine the operator-approved grants for this boundary and pass them through the installed CLI's documented grant mechanism; this adapter does not invent grant flags. Do not broaden the boundary on your own initiative.

## Resume

```sh
cd "<original workspace>" && dsh --profile headless --json --session-id "<builder-session-id>" - < "<run-dir>/turn-002-delta.txt" > "<run-dir>/turn-002-events.jsonl" 2> "<run-dir>/turn-002.err"
```

`--session-id` resumes the recorded session. DSH rejects unknown sessions and cwd, preset, or ownership mismatches. Repeat the explicit permission/approval/add-dir grants on resume using the same documented grant syntax; do not assume they are inherited. A rejected resume is a session/boundary failure: preserve the work and report it, do not cold-start.

## Completion, final message, and usage

- Session identity: the opening `session` event's `sessionId` and the closing `final` event's `sessionId`. All observed values must agree; a conflict is an error. The id is opaque.
- Terminal state: require both process success and `final.reason.kind == "completed"`. Reject `error`, `failed`, `interrupted`, missing, or incomplete final events. Process exit zero alone is not sufficient.
- Final message: the final answer carried by the `final` event. Do not use a reasoning or tool event as the final response. The runtime adapter accepts the documented `text`/`answer`/`content`/`message` projections and fails explicitly if none is present.
- Usage and cost: DSH exposes no documented token or cost payload in this projection. The runtime adapter records `usage_status: "unavailable"`, `cost_status: "unavailable"`, and denominator `unavailable`. Never infer usage from reasoning or tool events, and never convert token-looking numbers in non-usage events into an invoice.

Run the installed runtime adapter after every turn:

```sh
python "<installed-prewalk>/scripts/prewalk_runtime.py" inspect --host dsh --builder-turn <N> --events "<run-dir>/turn-NNN-events.jsonl" --final-message "<run-dir>/turn-NNN-final.txt" --usage "<run-dir>/usage.json"
```

## Validation status

DSH is not installed in this environment. Its parser is fixture-tested only; there is no live DSH execution evidence. Treat the command shape, profile loading, grant repetition, and projection fields as capabilities to verify against the operator's installed DSH version before claiming support.
