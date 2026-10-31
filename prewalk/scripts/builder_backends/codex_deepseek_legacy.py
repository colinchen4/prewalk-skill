"""codex-deepseek (legacy, optional): Codex Desktop + DeepSeek provider via the
codex-deepseek-subagent manager.  Kept for existing installs (macOS/Windows).
Not part of the default pairing chain; select it explicitly with
`prewalk setup --backend codex-deepseek` or `"backend": "codex:deepseek"` in brief.json.

The manager owns the DeepSeek key in the OS credential store; Prewalk only asks it
for `status --json` (read-only) and never sees the key.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .base import Auth, Backend, BackendError, Detection, Launch, run_probe

MODEL = "deepseek-flash"
PROVIDER = "deepseek"


def manager_script() -> Path | None:
    override = os.environ.get("PREWALK_CODEX_DEEPSEEK_MANAGER")
    if override == "none":          # tests / users who want the legacy path hidden
        return None
    candidates = [Path(override)] if override else []
    here = Path(__file__).resolve()
    # <repo>/prewalk/scripts/builder_backends/ -> <repo>/codex-deepseek-subagent/scripts/
    candidates.append(here.parents[3] / "codex-deepseek-subagent" / "scripts" / "codex_deepseek.py")
    candidates.append(Path.home() / ".codex" / "skills" / "codex-deepseek-subagent" / "scripts" / "codex_deepseek.py")
    for c in candidates:
        if c.is_file():
            return c
    return None


class CodexDeepseekLegacy(Backend):
    id, track, family, binary, runtime_host = "codex-deepseek", "legacy", "codex", "codex", "codex"
    weak_model, weak_effort = MODEL, "medium"
    supported_efforts = ("low", "medium", "high")
    in_default_chain = False
    validation = "legacy manager; Codex Desktop on macOS/Windows only"
    login_hint = "python3 codex-deepseek-subagent/scripts/codex_deepseek.py setup"

    def _status(self) -> dict:
        script = manager_script()
        if not script:
            return {"status": "not_installed"}
        code, out = run_probe([sys.executable, str(script), "status", "--json"], timeout=20)
        try:
            doc = json.loads(out)
            return doc if isinstance(doc, dict) else {"status": "unknown"}
        except ValueError:
            return {"status": "unknown", "detail": out[:200]}

    def detect(self) -> Detection:
        script = manager_script()
        return Detection(bool(script), str(script) if script else None, None)

    def auth_check(self, detection: Detection | None = None) -> Auth:
        st = self._status()
        if st.get("status") == "configured":
            return Auth("ready", "api_key", "manager status: configured (key in OS credential store)")
        return Auth("needs_key", "api_key", f"manager status: {st.get('status')}; run: {self.login_hint}")

    def check_model(self, model):
        if model and model != MODEL:
            raise BackendError(f"codex-deepseek: model is fixed to {MODEL}")
        return MODEL

    def launch(self, *, turn, workdir, brief_path, model=None, effort=None, session=None,
               writable_dirs=(), allowed_bash=()) -> Launch:
        self.check_model(model)
        st = self._status()
        if st.get("status") != "configured":
            raise BackendError("codex-deepseek: manager is not configured; run: " + self.login_hint)
        exe = ((st.get("checks") or {}).get("desktop_codex_path")) or self._exe()
        requested = effort or self.weak_effort
        eff = self.resolve_effort(requested)
        if turn == 1:
            argv = [exe, "exec", "--json", "-s", "workspace-write", "-C", str(workdir), "-m", MODEL,
                    "-c", f'model_provider="{PROVIDER}"']
            for d in writable_dirs:
                argv += ["--add-dir", str(d)]
        else:
            if not session:
                raise BackendError("codex-deepseek: resume requires the recorded thread id")
            argv = [exe, "exec", "resume", "--json", session, "-m", MODEL,
                    "-c", f'model_provider="{PROVIDER}"']
        argv += ["-c", f'model_reasoning_effort="{eff}"', "-"]
        return Launch(argv, cwd=str(workdir), stdin_path=str(brief_path), model=MODEL,
                      requested_effort=requested, effective_effort=eff, session=session)
