# Async Builder mode (opt-in)

Default Prewalk is serial and blocking. Use async mode only when the user asks for it or Navigator has independent work (planning, answering the user, reviewing landed edits). There is still exactly one Builder and one run directory per task; never run two Builder turns against one session.

## Launch

Write `brief.json` (≤1500-char `context`):

```json
{"handoff_id":"h1","host":"codex","cwd":"<workspace>","goal":"...","success":["..."],"scope":["src/x/"],
 "context":"first edit + retained decisions, short","argv":["<adapter initial command argv>"],
 "resume_argv":["<adapter resume argv, may use {session} {brief}>"],"stdin_brief":true}
```

`argv` comes verbatim from `adapters/<host>.md`; placeholders `{brief} {progress} {steer} {events} {err} {final} {cwd} {session}` are expanded. Then:

```sh
python scripts/prewalk_async.py launch --run-dir <run-dir> --brief brief.json   # returns at once
```

A detached supervisor runs the Builder, writes `state.json`, appends `started`/terminal events to `progress.jsonl`, and on exit runs `prewalk_runtime.inspect --handoff-id`, so the return marker and HANDOFF_ID are checked by code. Updates continue after the parent turn ends.

## Git layers (first edit in, evidence out)

When `cwd` is a git worktree (opt out with `"layers": false`), launch snapshots the worktree as layer **L1** over **L0 = HEAD** and appends to `brief.txt` the L1 manifest (A/M/D, +/-), a capped `git diff L0 L1` (`first_edit_diff_chars`, default 6000, with a truncation note pointing at `first-edit.patch`), and the fixed rule that L1 is the template layer. Snapshot L1 yourself first with `prewalk_layers.py snapshot --layer L1 --message "..."` to name it; launch then reuses it.

The supervisor (not the Builder) snapshots **L2-<n>** after each batch of new Builder progress lines and once more on exit (crash and cancel included), then writes `review.md`/`review.json` and puts the path in `state.json` and `status` (`review_report`). Snapshots use a temp `GIT_INDEX_FILE` seeded from a copy of the real index, so only changed or untracked files are rehashed and `.gitignore` is honoured; HEAD, branch, the real index and the worktree are never touched. Only `refs/prewalk/<handoff-id>/*` are written. Regenerate with `prewalk_layers.py review-report --run-dir D` (alias `layer-diff`); drop the refs with `prewalk_layers.py cleanup --handoff-id <id> --run-dir D` (objects go with `git gc`).

## Check back, redirect, cancel

- `status --run-dir D --after <last_seq>`: non-blocking; returns state and only new events. Keep `last_seq`; do not re-read the whole file.
- `steer --run-dir D --text "..."`: soft redirect; the Builder reads `steer.md` before each step.
- `cancel --run-dir D`: SIGTERM then SIGKILL the Builder process group; worktree is kept, state `cancelled`.
- `wait --run-dir D`: blocking path, equivalent to serial mode.

Review incrementally from `changed_paths` in new events (or the per-step layers in `layers.json`), but final acceptance still needs the terminal `completed` state, the extracted final message, and the actual diff (Phase C). For a correction after a terminal state, write `turn-002-delta.txt` and `launch --turn 2 --expected-session <id>`; never cold-start.
