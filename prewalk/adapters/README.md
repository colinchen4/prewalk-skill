# Harness adapters

Prewalk core is independent of the Navigator harness, Builder harness, model vendor, and provider. An adapter supplies those host-specific bindings. Navigator and Builder may use different harnesses and models. Do not infer support for a harness merely because it can execute a prompt.

## Built-in adapters

| Host id | Adapter | Validation status |
| --- | --- | --- |
| `codex` | [codex.md](codex.md) | Live-validated in prior managed Codex runs; not re-run by the harness-neutral change. |
| `kimi` | [kimi.md](kimi.md) | Live-validated in prior local Kimi Code runs; not re-run by the harness-neutral change. |
| `claude-code` | [claude-code.md](claude-code.md) | Installed CLI inspected and an unauthenticated probe rejected; success/resume paths are fixture-tested only. |
| `dsh` | [dsh.md](dsh.md) | Fixture-tested only; DSH is not installed here. |
| `generic` | [this file](#other-harnesses-normalized-event-shim) | Contract plus fixture tests. Requires a thin host shim; not automatic support. |

Core selects one adapter before dispatch and reads that file for launch, resume, permission, setup, and artifact details. A missing capability fails explicitly.

## Adapter contract

Each adapter must document and, where automated, validate:

- how the core skill/instructions are loaded by that harness;
- first-use provider/model/endpoint/credential setup through native or managed configuration, without secrets in prompts or artifacts;
- initial and resume command shapes, working-directory and permission boundaries;
- where the opaque Builder session ID and final assistant message come from;
- native event/progress output and its completeness limits;
- token/cache/cost semantics, including cumulative versus per-turn values and whether cost is reported, estimated, or unavailable;
- compaction/model/effort switching capabilities and explicit unsupported behavior.

Core orchestration must select an adapter and read it before dispatch. Missing capabilities fail explicitly; they must not be replaced with invented flags, session IDs, usage, cache hits, or costs. A host without resumable sessions cannot satisfy the continuity contract through repeated cold starts.

Built-in bindings have separate files for Codex, Kimi Code, Claude Code, and DeepSeek Harness (DSH). Their validation status is independent: fixture-tested is not live-tested, and an adapter for one harness proves nothing about another.

## Other harnesses: normalized event shim

For another coding harness, keep its native raw output unchanged and add a thin shim that emits a separate normalized JSONL turn artifact. Pass that artifact to `prewalk_runtime.py inspect --host generic`. The shim and adapter document own launch, resume, permission, and setup behavior; core does not execute an unverified command template.

The normalized envelope is:

```json
{"type":"session","session_id":"opaque-builder-session"}
{"type":"usage","model":"optional-model-name","scope":"turn","usage":{"input_other":100,"cache_read":50,"cache_creation":25,"output":10,"reasoning_output":2},"cost":null,"cost_status":"unavailable","cost_scope":"call"}
{"type":"final","session_id":"opaque-builder-session","text":"final assistant response","completed":true}
```

When the native API defines cached tokens as included in total input, emit the included-total family instead:

```json
{"type":"usage","usage":{"generic_input_tokens":1000,"generic_cached_input_tokens":800,"output":10},"cost":null,"cost_status":"unavailable"}
```

Requirements:

- Exactly one logical session ID per turn artifact; a resumed turn must match `--expected-session`.
- `session` may occur at the beginning and `final` at the end; both may repeat only with the same ID.
- `final.text` is the last complete assistant response, not a reasoning transcript. `completed` must reflect the harness terminal state, not merely process exit zero.
- Zero or more `usage` events represent model calls in this Builder turn. Token values are non-negative numbers. A usage `scope` is optional; when present it must be `turn`. The normalized envelope is per Builder turn, so session-scope records are rejected rather than aggregated.
- Cache tokens have two possible denominator families. Use the separate family by default: `input_other`, `cache_read`, and `cache_creation`, where the ratio denominator is `input_other+cache_read+cache_creation`. Use the included-total family only when the native API defines cached tokens as included in total input: `generic_input_tokens` and `generic_cached_input_tokens`, where the ratio denominator is `generic_input_tokens`. Do not mix families in one artifact, and never compare a ratio with another adapter without its denominator label.
- `cost` is `null`, a non-negative number, or an object with a non-negative numeric `amount` and optional string `currency`. `cost_status` is `reported` for provider-reported charges, `estimated` for client estimates, or `unavailable`. Never convert token counts into cost inside the shim unless that estimate is explicitly labeled.
- `cost_scope` is `call` or `session_cumulative`; omit it only when the value is per-call. A `session_cumulative` value already covers earlier calls in the same session, so the runtime adapter retains the latest cumulative value instead of summing it across calls or turns.
- `cost_status: "unavailable"` requires `cost: null`; `reported` or `estimated` requires a valid cost. Invalid status, scope, cost, or token values fail the inspection or are explicitly marked unavailable; they are never repaired into plausible data.
- Preserve native events separately as `turn-NNN-native.jsonl` (or the host's native format); normalized events are `turn-NNN-events.jsonl`. Never overwrite earlier turns.

If a harness cannot provide session continuity or final state, report `unavailable` and stop at that boundary. Do not fabricate a normalized record to make an unsupported host appear compatible.

## Implementing another harness

1. Keep the host's native launch, resume, permission, and setup behavior in its own adapter document. Do not add harness commands to `SKILL.md`.
2. Emit native output unchanged as the native artifact, and translate only the fields above into a separate normalized JSONL file.
3. Prove session identity and terminal completion from the harness, not from process exit alone.
4. Declare each capability honestly: live-tested, fixture-tested, or unverified. A shim that cannot observe usage must emit no usage events and let the runtime report `unavailable`.

Pass the normalized artifact to `prewalk_runtime.py inspect --host generic`; see the [runtime script](../scripts/prewalk_runtime.py) and its tests for accepted shapes.
