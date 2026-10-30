# Builder backends

The Builder is a second CLI process on a **weak model**. `scripts/builder_backends/` is the registry; `scripts/prewalk.py doctor` reads it, `scripts/prewalk_async.py` launches through it.

| id | track | CLI / model (default, alt) | effort | status |
| --- | --- | --- | --- | --- |
| `claude-code-sub` | A, Claude Pro/Max login | `claude -p --model sonnet` (`haiku`) | `--effort`, default `medium` | fixture-tested |
| `codex-sub` | A, ChatGPT login | `codex exec -m gpt-6-luna` (`gpt-6.1-terra`) | `-c model_reasoning_effort=…`, default `high` | fixture-tested; effort key unverified |
| `kimi-sub` | A, Kimi membership | `kimi acp` / `kimi -m kimi-code/kimi-for-coding` (`kimi-code/k3-256k`) | ACP `low/high/max`, default `low` | persistent ACP and OAuth live-verified |
| `opencode-api` | B, external API | opencode + DeepSeek | – | stub |
| `pi-api` | B, external API | pi + DeepSeek | – | stub |
| `codex-deepseek` (`codex:deepseek`) | legacy | Codex Desktop + DeepSeek manager | low/medium/high | optional, explicit only |

Track A uses the user's own logged-in official CLI and never an API key: each backend removes the API-key/gateway variables from the Builder environment (`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_EFFORT_LEVEL`, …; `OPENAI_API_KEY`, `CODEX_API_KEY`). Prewalk never reads, stores, or forwards OAuth tokens or keys; `~/.prewalk/config.json` holds only backend/model/effort/concurrency.

## Blocked as Builder

- Claude: `best`, `fable`, `opus*`, `opusplan`, `default` (Navigator-class or usage-credit models).
- Codex: Sol and Astra (about 1/20 of Luna's quota).
- Kimi: `kimi-for-coding-highspeed` (3x quota).

## Model discovery and optional diagnostics

```sh
python3 scripts/prewalk.py models --navigator-host kimi   # read-only usable model/effort choices; no config writes
python3 scripts/prewalk.py doctor                         # optional readiness diagnosis
python3 scripts/prewalk.py setup                          # optional saved Builder default
python3 scripts/prewalk.py test                           # optional round trip in a throwaway git repo
```

There is no mandatory first-run setup. Navigator defaults to the current session's model and effective effort; Builder is chosen from `models` with `medium` effort when supported, otherwise effort is recorded as `null`. `models` includes implemented, installed, ready backends and never returns API-key Track A logins as subscription choices. Kimi accepts a managed provider reported by `kimi provider list` with `type=kimi` and `source=oauth`, so an existing Desktop login can be reused without another login. If no managed OAuth provider is listed, choose another backend or run `kimi login` only when needed. Builder concurrency is 1 by default (`setup --max-concurrency 2` at most).

## Effort

Canonical values `minimal low medium high xhigh max`. An unsupported request resolves to the nearest supported level at or below it (never up); a request below the backend's minimum supported level is rejected. Both values are recorded (`requested_effort`, `effective_effort`). Kimi's legacy one-shot interface has no effort flag and records `null`; its persistent ACP interface supports `low high max` and defaults to `low`.

## Limits

A quota or rate-limit exit (`You've hit your … limit`, 429, `rate_limit_exceeded`, Kimi exit 75) is the retryable state `waiting_rate_limit`, not a failure. Retry after the reset with `prewalk_async.py retry --run-dir D`, or switch model (`--model haiku`) or backend (`--backend codex-sub`).

## Not verified live (phase 1)

Subscription logins in `-p`/`exec` mode, the Codex effort config key, Luna availability per account, two concurrent `codex exec` on one login, `codex exec --add-dir`, Claude `Bash(prefix *)` rule granularity, the Kimi model alias / login probe / exit 75.
