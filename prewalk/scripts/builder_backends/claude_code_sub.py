"""claude-code-sub: a second `claude -p` on the user's Pro/Max login, on a weaker model.

VERIFIED (code.claude.com/docs/en/model-config, 2026-10-30):
  - --model applies only to the launched session; running different models in
    different terminals at the same time is documented usage.
  - Aliases sonnet / haiku / opus / best / fable / default; `default` is Opus on Pro/Max.
  - 5.5 models accept --effort low|medium|high|xhigh|max; CLAUDE_CODE_EFFORT_LEVEL
    overrides --effort, so it is stripped from the Builder env.
  - --resume keeps the transcript model unless --model is passed again.
VERIFIED (cli-reference / headless): `claude auth status` prints JSON with authMethod and
  exits non-zero when logged out; since 2.1.302 `-p` starts in Manual permission mode,
  so --permission-mode acceptEdits plus an explicit --allowedTools list is required.
UNVERIFIED (needs a live run): precedence of ANTHROPIC_API_KEY over OAuth in -p (we strip
  it), and the exact granularity of Bash(...) allow rules.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Iterable

from .base import (BOOTSTRAP, BOOTSTRAP_RESUME, Auth, Backend, Detection, Launch, run_probe)

# Edit/read tools the Builder needs; Bash is only granted per command prefix.
BASE_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep", "TodoWrite",
              "Bash(git status *)", "Bash(git diff *)", "Bash(git log *)")


class ClaudeCodeSub(Backend):
    id, track, family, binary, runtime_host = "claude-code-sub", "A", "claude-code", "claude", "claude-code"
    weak_model, weak_effort = "sonnet", "medium"
    alt_models = ("haiku",)
    # `default`/`opus*` is what the Navigator usually runs; `best`/`fable` bill usage
    # credits on Pro silently in -p mode.  None of them may be the Builder.
    blocked_models = ("best", "fable", "opus", "opusplan", "default")
    supported_efforts = ("low", "medium", "high", "xhigh", "max")
    env_remove = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                  "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_*",
                  "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                  "CLAUDE_CODE_USE_FOUNDRY")
    min_version = "2.1.302"          # -p Manual-mode default; haiku 5.5 needs >= 2.1.293
    cancel_signals = ("SIGINT", "SIGTERM", "SIGKILL")   # SIGINT ends the turn cleanly
    validation = "fixture-tested (argv/env snapshots); not live-run"
    login_hint = "claude auth login   (sign in with your Claude Pro/Max account)"

    def auth_check(self, detection: Detection | None = None) -> Auth:
        exe = (detection.path if detection else None) or self._exe()
        code, out = run_probe([exe, "auth", "status"], env=self.probe_env())
        if code != 0:
            return Auth("needs_login", "unknown", "run: " + self.login_hint)
        try:
            doc = json.loads(out)
            method = doc.get("authMethod", "unknown") if isinstance(doc, dict) else "unknown"
        except ValueError:
            method = "unknown"
        billing = {"claude.ai": "subscription", "oauth_token": "subscription",
                   "api_key": "api_key", "api_key_helper": "api_key",
                   "third_party": "third_party"}.get(method, "unknown")
        detail = f"authMethod={method}"
        if billing != "subscription":
            detail += "; Track A expects a Pro/Max login (claude auth login)"
        return Auth("ready", billing, detail)

    def new_session_id(self) -> str:
        return str(uuid.uuid4())

    @staticmethod
    def allowed_tools(allowed_bash: Iterable[str] = ()) -> list[str]:
        tools = list(BASE_TOOLS)
        for cmd in allowed_bash:
            cmd = " ".join(str(cmd).split())
            if cmd and not any(ch in cmd for ch in "();&|`$<>"):
                rule = f"Bash({cmd} *)"
                if rule not in tools:
                    tools.append(rule)
        return tools

    def launch(self, *, turn, workdir, brief_path, model=None, effort=None, session=None,
               writable_dirs=(), allowed_bash=()) -> Launch:
        model = self.check_model(model)
        requested = effort or self.weak_effort
        eff = self.resolve_effort(requested, model)
        prompt = (BOOTSTRAP if turn == 1 else BOOTSTRAP_RESUME).format(brief=brief_path)
        argv = [self._exe(), "-p", prompt, "--output-format", "stream-json", "--verbose",
                "--permission-mode", "acceptEdits",
                "--allowedTools", *self.allowed_tools(allowed_bash),
                "--permission-prompts", "none"]
        for d in writable_dirs:
            argv += ["--add-dir", str(d)]
        if turn == 1:
            argv += ["--model", model]
            if session:
                argv += ["--session-id", session]
        else:
            if not session:
                from .base import BackendError
                raise BackendError("claude-code-sub: resume requires the recorded session id")
            argv += ["--resume", session, "--model", model]   # --model wins over transcript model
        if eff:
            argv += ["--effort", eff]
        notes = []
        if requested and eff != requested:
            notes.append(f"effort {requested} not supported by {model}; using {eff}")
        return Launch(argv, cwd=str(workdir), env_remove=self.env_remove, model=model,
                      requested_effort=requested, effective_effort=eff, session=session, notes=notes)

    def observed_models(self, events_path: Path) -> list[str]:
        models: list[str] = []
        try:
            lines = Path(events_path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return models
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init" and isinstance(ev.get("model"), str):
                models.append(ev["model"])
            if ev.get("type") == "result" and isinstance(ev.get("modelUsage"), dict):
                models.extend(k for k in ev["modelUsage"] if isinstance(k, str))
        return sorted(set(models))
