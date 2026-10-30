"""kimi-sub: a second `kimi -p` on the user's Kimi membership (Plus and above). Basic.

VERIFIED (kimi.com/code/docs, 2026-10-30): -p, --output-format stream-json, -m <alias>,
  -S/--session <id>; -p runs under the auto permission policy; kimi-for-coding (K2.8)
  is available from Plus.
UNVERIFIED: there is no CLI effort flag (effort comes from config); the managed alias
  name for -m; a read-only login probe; whether exit 75 means "retry later".
"""

from __future__ import annotations

import os

from .base import BOOTSTRAP, BOOTSTRAP_RESUME, Auth, Backend, BackendError, Detection, Launch


class KimiSub(Backend):
    id, track, family, binary, runtime_host = "kimi-sub", "A", "kimi", "kimi", "kimi"
    weak_model, weak_effort = "kimi-code/kimi-for-coding", None
    alt_models = ("kimi-code/k3-256k",)
    blocked_models = ("kimi-for-coding-highspeed",)    # 3x quota: not a cheap Builder
    supported_efforts = ()
    retry_exit_codes = (75,)                            # UNVERIFIED
    validation = "basic; launch shape from the live-validated kimi adapter, alias unverified"
    login_hint = "kimi   (then /login with your Kimi membership)"

    def auth_check(self, detection: Detection | None = None) -> Auth:
        home = os.environ.get("KIMI_CODE_HOME") or os.path.expanduser("~/.kimi-code")
        if os.path.exists(os.path.join(home, "config.toml")):
            return Auth("unknown", "subscription",
                        "config found; login state is not probeable read-only (UNVERIFIED)")
        return Auth("needs_login", "unknown", "run: " + self.login_hint)

    def launch(self, *, turn, workdir, brief_path, model=None, effort=None, session=None,
               writable_dirs=(), allowed_bash=()) -> Launch:
        model = self.check_model(model)
        argv = [self._exe()]
        if turn > 1:
            if not session:
                raise BackendError("kimi-sub: resume requires the recorded session id")
            argv += ["--session", session]
        prompt = (BOOTSTRAP if turn == 1 else BOOTSTRAP_RESUME).format(brief=brief_path)
        argv += ["-m", model, "-p", prompt, "--output-format", "stream-json"]
        notes = [f"kimi has no effort flag; requested {effort} is not applied"] if effort else []
        return Launch(argv, cwd=str(workdir), model=model, requested_effort=effort,
                      effective_effort=None, session=session, notes=notes)
