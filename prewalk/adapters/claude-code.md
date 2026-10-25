# Claude Code adapter

Host id: `claude-code`. Use `prewalk_runtime.py inspect --host claude-code`.

Claude Code is one harness binding for the Prewalk core. The installed CLI observed for this adapter is 2.1.172.

## Load the core instructions

Install `prewalk/` as a Claude Code skill (`~/.claude/skills/prewalk/` for a user-scope install or `<project>/.claude/skills/prewalk/` for a project). Claude Code reads `SKILL.md` and follows the harness-neutral orchestration; this adapter supplies launch, resume, artifact, and usage details.

Do not use `--bare` by default. Repository instructions (`CLAUDE.md`, `AGENTS.md`, and project context) matter to the Builder, and `--bare` skips the normal instruction discovery. Use it only when the operator explicitly wants a repository-instruction-free boundary and understands the loss.

## Setup and permissions

- Authenticate with the user's native Claude Code flow (`/login`). Never put credentials, tokens, or login output in a handoff, progress file, log, or commit. Do not log in on the operator's behalf during a Prewalk implementation run.
- Use the same permission boundary for the initial turn and every resume. Append only permission flags that the installed version supports and the operator approved; validate them with `claude --version` and the CLI's own help before relying on a flag. Do not guess new flag names, and do not silently broaden or narrow the boundary on resume.
- `--model` and `--effort` exist on the observed CLI, but ordinary resumes omit model/effort changes and inherit the recorded boundary. A requested model or effort change is a new pairing decision, not a hidden default.
- `--add-dir` can extend the workspace; if the initial boundary needed it, carry the same grant on resume. Do not invent a grant that was not part of the recorded launch.

## Initial dispatch

```sh
cd "<task workspace>" && claude -p "<short bootstrap prompt that references the absolute handoff package file>" --output-format stream-json --verbose > "<run-dir>/turn-001-events.jsonl" 2> "<run-dir>/turn-001.err"
```

`--verbose` is required with `--output-format stream-json` so the stream carries the terminal result metadata. Append the validated permission flags for the chosen boundary. The bootstrap prompt tells the child to read the handoff file and return the standard `PREWALK_PHASE: NAVIGATOR_REVIEW` package (legacy `SOL_REVIEW` is accepted during migration) with the same `HANDOFF_ID`.

`--output-format json` emits a single result object instead of a stream; the runtime adapter accepts either a JSONL stream or a one-line JSON object. Prefer `stream-json` for a turn artifact.

## Resume

```sh
cd "<original workspace>" && claude -p --resume "<builder-session-id>" "<short delta prompt that references the absolute delta file>" --output-format stream-json --verbose > "<run-dir>/turn-002-events.jsonl" 2> "<run-dir>/turn-002.err"
```

Use the same workspace, model, effort, and permission boundary recorded in `run.json`. Do not add model/effort changes on an ordinary resume. Do not add flags that were not validated for the installed version. A resume failure is a routing/session failure: preserve the work and report it; do not cold-start.

## Session, final message, and error semantics

- Session id: the terminal `result.session_id`, cross-checked against any `system` event with `subtype: "init"`. All observed non-empty values must agree; a conflict is an error. The id is opaque.
- Final message: `result.result`, and only when `is_error` is exactly `false`. Do not treat the result text as success when `is_error` is true, even if `subtype` says `success` and the text looks like an answer.
- A real unauthenticated probe produced:
  `{"type":"result","subtype":"success","is_error":true,"result":"Not logged in · Please run /login","session_id":"...","total_cost_usd":0,"usage":{"input_tokens":0,"cache_read_input_tokens":0,"cache_creation_input_tokens":0,"output_tokens":0},"modelUsage":{}}`
  The adapter rejects this result and writes no final-message or usage artifact. Authentication is an operator action; do not request credentials during the implementation.
- Usage fields observed: `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, and `output_tokens`. For Anthropic-style usage the cached counts are separate from noncached `input_tokens`, so the cache-read ratio denominator is `input_tokens+cache_read_input_tokens+cache_creation_input_tokens`. Zero counts are provider-reported zeros, not missing data; absent, empty, or non-object usage makes token usage `unavailable` or `partial`.
- `total_cost_usd` is a client-side estimate, not an invoice, and is cumulative across resumed runs in the same session. Cost evidence is parsed independently of token evidence: a valid `total_cost_usd` is retained even when the `usage` object is absent, empty, or malformed, as a cost-only call with `usage: {}` and partial token observability. The adapter records `cost_status: "estimated"` and `cost_scope: "session_cumulative"`, and retains the latest cumulative value across turns. Never sum it across turns and never present it as provider-reported billing. An invalid or missing cost remains `unavailable`.
- `modelUsage` may be empty (as in the error probe). A sole model key is used as the call's model label; do not infer usage or cost from an empty object.

Run the installed runtime adapter after every turn:

```sh
python "<installed-prewalk>/scripts/prewalk_runtime.py" inspect --host claude-code --builder-turn <N> --events "<run-dir>/turn-NNN-events.jsonl" --final-message "<run-dir>/turn-NNN-final.txt" --usage "<run-dir>/usage.json"
```

## Validation status

- CLI version 2.1.172 was installed and inspected.
- A real unauthenticated `claude -p` probe returned the not-logged-in result shown above; the parser's rejection path is fixture-tested and was also exercised against that real probe artifact.
- No authenticated Claude Code live execution is available in this environment. The success path, resume path, permission continuity, and cost semantics are fixture-tested only. Do not describe this adapter as live-tested end to end.
