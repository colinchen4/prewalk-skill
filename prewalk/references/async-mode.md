# Async Builder mode (opt-in)

Default Prewalk is serial and blocking. Use async mode only when the user asks for it or Navigator has independent work (planning, answering the user, reviewing landed edits). There is still exactly one Builder and one run directory per task; never run two Builder turns against one session.

## Launch

Write `brief.json` (≤1500-char `context`). Preferred: name a registry backend ([backends.md](backends.md); `prewalk.py doctor` recommends one) and let it build argv/env:

```json
{"handoff_id":"h1","backend":"claude-code-sub","model":"sonnet","effort":"medium",
 "cwd":"<builder worktree>","goal":"...","success":["..."],"scope":["src/x/"],
 "context":"first edit + retained decisions, short","allowed_bash":["python3 -m unittest"]}
```

`cwd` is the Builder worktree for every backend; the run directory is granted as an extra writable dir so the Builder can append `progress.jsonl`. `allowed_bash` lists command prefixes the Claude Builder may run (no blanket Bash). `model`/`effort` default to the backend's weak model; blocked models are rejected at launch.

Generic override (older briefs, custom harnesses): a literal argv still works and wins over `backend`:

```json
{"handoff_id":"h1","host":"codex","cwd":"<workspace>","goal":"...","success":["..."],"scope":["src/x/"],
 "argv":["<adapter initial command argv>"],"resume_argv":["<adapter resume argv, may use {session} {brief}>"],"stdin_brief":true}
```

`argv` comes verbatim from `adapters/<host>.md`; placeholders `{brief} {progress} {steer} {events} {err} {final} {cwd} {session}` are expanded. Then:

```sh
python scripts/prewalk_async.py launch --run-dir <run-dir> --brief brief.json   # returns at once
```

A detached supervisor runs the Builder (environment minus the backend's API-key variables; `PREWALK_PROGRESS`/`PREWALK_STEER`/`PREWALK_RUN_DIR` added), records `backend`, `model`, `requested_effort` and `effective_effort` in `state.json`, appends `started`/terminal events to `progress.jsonl`, and on exit runs `prewalk_runtime.inspect --handoff-id`, so the return marker and HANDOFF_ID are checked by code. Updates continue after the parent turn ends.

## Git layers (first edit in, evidence out)

When `cwd` is a git worktree (opt out with `"layers": false`), capture **L0 before the Navigator's first edit** so inherited dirty-worktree changes have their own baseline:

```sh
python scripts/prewalk_layers.py snapshot --run-dir <run-dir> --handoff-id h1 --cwd <workspace> --layer L0 --message "inherited baseline"
```

Launch then snapshots **L1** over L0 and appends to `brief.txt` the L1 manifest (A/M/D, +/-), a capped `git diff L0 L1` (`first_edit_diff_chars`, default 6000, with a truncation note pointing at `first-edit.patch`), and the fixed rule that L1 is the template layer. To name L1 yourself, snapshot it after the first edit with `prewalk_layers.py snapshot --layer L1 --message "..."`; launch reuses it. Launch rejects a missing L0 rather than misattributing inherited changes to Navigator. `review.md`/`review.json` report inherited changes separately from the task diff.

The supervisor (not the Builder) snapshots **L2-<n>** after each batch of new Builder progress lines and once more on exit (crash and cancel included), then writes `review.md`/`review.json` and puts the path in `state.json` and `status` (`review_report`). Snapshots use a temp `GIT_INDEX_FILE` seeded from a copy of the real index, so only changed or untracked files are rehashed and `.gitignore` is honoured; HEAD, branch, the real index and the worktree are never touched. Only `refs/prewalk/<handoff-id>/*` are written. Regenerate with `prewalk_layers.py review-report --run-dir D` (alias `layer-diff`); drop the refs with `prewalk_layers.py cleanup --handoff-id <id> --run-dir D` (objects go with `git gc`).

## Check back, redirect, cancel

- `status --run-dir D --after <last_seq>`: non-blocking; returns state and only new events. Keep `last_seq`; do not re-read the whole file.
- `steer --run-dir D --text "..."`: soft redirect; the Builder reads `steer.md` before each step.
- `cancel --run-dir D`: signals the Builder process group with the backend's sequence (default SIGTERM→SIGKILL; Claude SIGINT→SIGTERM→SIGKILL; Windows CTRL_BREAK then `taskkill /T /F`); worktree is kept, state `cancelled`.
- Rate/usage limit: state `waiting_rate_limit` with `retryable: true` and `limit_message`. `retry --run-dir D [--model M] [--backend B]` archives the attempt as `turn-NNN-attempt-K.*` and relaunches the same turn.
- One registry-backend Builder runs at a time per user (`~/.prewalk/active/`); raise with `prewalk.py setup --max-concurrency 2`.
- `wait --run-dir D`: blocking path, equivalent to serial mode.

Review incrementally from `changed_paths` in new events (or the per-step layers in `layers.json`), but final acceptance still needs the terminal `completed` state, the extracted final message, and the actual diff (Phase C). For a correction after a terminal state, write `turn-002-delta.txt` and `launch --turn 2 --expected-session <id>`; never cold-start.
