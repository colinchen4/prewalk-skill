"""codex-sub: a second `codex exec` on the user's ChatGPT login, on a lower-cost model.

VERIFIED (developers.openai.com/codex/models, 2026-10-30):
  - `codex exec -m <model>`; gpt-5.4-mini was replaced by gpt-6-luna; Luna supports
    efforts up to max (no ultra) and the docs suggest starting Luna at high.
  - Plus allows ~350-3000 Luna messages per 5h vs 15-160 for 6.1 Sol.
VERIFIED (developers.openai.com/codex/auth): `codex login status` exits 0 when logged in.
UNVERIFIED (needs a live run):
  - the effort override key `-c model_reasoning_effort="<level>"`;
  - Luna availability in the CLI for every Plus account ("subject to rollout");
  - two concurrent `codex exec` on one ChatGPT login;
  - `--add-dir` for the run directory under workspace-write.
"""

from __future__ import annotations

from .base import Auth, Backend, BackendError, Detection, Launch, run_probe

EFFORT_CONFIG_KEY = "model_reasoning_effort"   # UNVERIFIED key name (see module doc)


class CodexSub(Backend):
    id, track, family, binary, runtime_host = "codex-sub", "A", "codex", "codex", "codex"
    weak_model, weak_effort = "gpt-6-luna", "high"
    alt_models = ("gpt-6.1-terra",)
    blocked_models = ("astra", "sol")   # Navigator-class models; ~20x less quota than Luna
    supported_efforts = ("low", "medium", "high", "xhigh", "max")
    env_remove = ("CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL")   # stay on the ChatGPT login
    min_version = "0.159.0"          # gpt-6.1-terra fallback needs >= 0.159.0
    validation = "fixture-tested (argv/env snapshots); not live-run"
    login_hint = "codex login   (or: codex login --device-auth; sign in with ChatGPT)"

    def auth_check(self, detection: Detection | None = None) -> Auth:
        exe = (detection.path if detection else None) or self._exe()
        code, out = run_probe([exe, "login", "status"], env=self.probe_env())
        if code != 0:
            return Auth("needs_login", "unknown", "run: " + self.login_hint)
        low = out.lower()
        billing = "subscription" if "chatgpt" in low else ("api_key" if "api key" in low else "unknown")
        detail = out.splitlines()[0] if out else ""
        if billing == "api_key":
            detail += "; Track A expects a ChatGPT login (codex login)"
        return Auth("ready", billing, detail)

    def launch(self, *, turn, workdir, brief_path, model=None, effort=None, session=None,
               writable_dirs=(), allowed_bash=()) -> Launch:
        exe, model = self._exe(), self.check_model(model)
        requested = effort or self.weak_effort
        eff = self.resolve_effort(requested, model)
        if turn == 1:
            argv = [exe, "exec", "--json", "-s", "workspace-write", "-C", str(workdir), "-m", model]
            for d in writable_dirs:
                argv += ["--add-dir", str(d)]
        else:
            if not session:
                raise BackendError("codex-sub: resume requires the recorded thread id")
            # Resume inherits the recorded sandbox; only the model/effort pin is repeated.
            argv = [exe, "exec", "resume", "--json", session, "-m", model]
        if eff:
            argv += ["-c", f'{EFFORT_CONFIG_KEY}="{eff}"']
        argv.append("-")                                   # prompt (the brief) from stdin
        notes = []
        if requested and eff != requested:
            notes.append(f"effort {requested} not supported by {model}; using {eff}")
        return Launch(argv, cwd=str(workdir), env_remove=self.env_remove, stdin_path=str(brief_path),
                      model=model, requested_effort=requested, effective_effort=eff,
                      session=session, notes=notes)
