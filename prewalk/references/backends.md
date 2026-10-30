# Builder backends

The Builder is a second CLI process on a **weak model**. `scripts/builder_backends/` is the registry; `scripts/prewalk.py doctor` reads it, `scripts/prewalk_async.py` launches through it.

| id | track | CLI / model (default, alt) | effort | status |
| --- | --- | --- | --- | --- |
| `claude-code-sub` | A, Claude Pro/Max login | `claude -p --model sonnet` (`haiku`) | `--effort`, default `medium` | fixture-tested |
| `codex-sub` | A, ChatGPT login | `codex exec -m gpt-6-luna` (`gpt-6.1-terra`) | `-c model_reasoning_effort=…`, default `high` | fixture-tested; effort key unverified |
| `kimi-sub` | A, Kimi membership | `kimi -m kimi-code/kimi-for-coding` (`kimi-code/k3-256k`) | none (config) | basic |
| `opencode-api` | B, external API | opencode + DeepSeek | – | stub |
| `pi-api` | B, external API | pi + DeepSeek | – | stub |
| `codex-deepseek` (`codex:deepseek`) | legacy | Codex Desktop + DeepSeek manager | low/medium/high | optional, explicit only |

Track A uses the user's own logged-in official CLI and never an API key: each backend removes the API-key/gateway variables from the Builder environment (`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_EFFORT_LEVEL`, …; `OPENAI_API_KEY`, `CODEX_API_KEY`). Prewalk never reads, stores, or forwards OAuth tokens or keys; `~/.prewalk/config.json` holds only backend/model/effort/concurrency.

## Blocked as Builder

- Claude: `best`, `fable`, `opus*`, `opusplan`, `default` (Navigator-class or usage-credit models).
- Codex: Sol and Astra (about 1/20 of Luna's quota).
- Kimi: `kimi-for-coding-highspeed` (3x quota).

## First run

```sh
python3 scripts/prewalk.py doctor                 # read-only; --json; --navigator-host claude-code|codex|kimi|...
python3 scripts/prewalk.py setup                  # accept the recommendation (or --backend/--model/--effort)
python3 scripts/prewalk.py test                   # one round trip in a throwaway git repo
```

Navigator host: `--navigator-host`, else `PREWALK_HOST`, else auto-detect. Pairing: saved config if still ready → same family as Navigator → other ready Track A → ready Track B → fallback hint, exit 3. Automatic Track A recommendations require verified subscription billing, not an API-key login. Kimi is explicit-only in phase 1 (`setup --backend kimi-sub` followed by `prewalk.py test`) because it has no read-only login probe; it is not auto-recommended or reused as a ready saved pairing. Same-family Builders share the Navigator's plan quota; the weak model is what keeps that cheap. Builder concurrency is 1 by default (`setup --max-concurrency 2` at most).

## Effort

Canonical values `minimal low medium high xhigh max`. An unsupported request resolves to the nearest supported level at or below it (never up); a request below the backend's minimum supported level is rejected. Both values are recorded (`requested_effort`, `effective_effort`). Kimi has no effort flag: effective is `null`.

## Limits

A quota or rate-limit exit (`You've hit your … limit`, 429, `rate_limit_exceeded`, Kimi exit 75) is the retryable state `waiting_rate_limit`, not a failure. Retry after the reset with `prewalk_async.py retry --run-dir D`, or switch model (`--model haiku`) or backend (`--backend codex-sub`).

## Not verified live (phase 1)

Subscription logins in `-p`/`exec` mode, the Codex effort config key, Luna availability per account, two concurrent `codex exec` on one login, `codex exec --add-dir`, Claude `Bash(prefix *)` rule granularity, the Kimi model alias / login probe / exit 75.
