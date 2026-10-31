"""kimi-sub: a second `kimi -p` on the user's Kimi membership (Plus and above). Basic.

VERIFIED (kimi.com/code/docs, 2026-10-30): -p, --output-format stream-json, -m <alias>,
  -S/--session <id>; -p runs under the auto permission policy; kimi-for-coding (K2.8)
  is available from Plus. On CLI 0.43.1, `kimi provider list` exposes the managed
  provider's `source=oauth` without printing credentials, including Desktop logins.
  ACP `session/set_config_option` exposes thinking low|high|max; one-shot `-p`
  has no effort flag. UNVERIFIED: the managed alias name for -m and exit 75 semantics.
"""

from __future__ import annotations

import os

from .base import BOOTSTRAP, BOOTSTRAP_RESUME, Auth, Backend, Detection, Launch, run_probe


class KimiSub(Backend):
    id, track, family, binary, runtime_host = "kimi-sub", "A", "kimi", "kimi", "kimi"
    weak_model, weak_effort = "kimi-code/kimi-for-coding", "low"
    alt_models = ("kimi-code/k3-256k",)
    blocked_models = ("kimi-for-coding-highspeed",)    # 3x quota: not a cheap Builder
    supported_efforts = ("low", "high", "max")
    persistent = True
    retry_exit_codes = (75,)                            # UNVERIFIED
    validation = "persistent ACP and managed OAuth live-verified; one-shot alias unverified"
    login_hint = "kimi login   (only when provider list has no managed OAuth provider)"

    def auth_check(self, detection: Detection | None = None) -> Auth:
        exe = (detection.path if detection else None) or self._exe()
        code, out = run_probe([exe, "provider", "list"], env=self.probe_env())
        if code == 0:
            for line in out.splitlines():
                fields = line.split()
                if line.strip().startswith("managed:") and "type=kimi" in fields and "source=oauth" in fields:
                    return Auth("ready", "subscription", "managed Kimi OAuth provider found")
        home = os.environ.get("KIMI_CODE_HOME") or os.path.expanduser("~/.kimi-code")
        if os.path.exists(os.path.join(home, "config.toml")):
            return Auth("unknown", "subscription", "config found; managed OAuth provider not detected")
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
