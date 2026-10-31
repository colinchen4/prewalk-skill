"""Track B registry stubs: open agents (opencode, pi) on an external API (DeepSeek).

Listed by `prewalk doctor` so users can see the option, but launch is not
implemented in phase 1 and they are never auto-recommended.  Keys stay in the
open agent's own provider config / environment; Prewalk does not read them.
"""

from __future__ import annotations

import os

from .base import Auth, Backend, Detection, run_probe


class OpenCodeApi(Backend):
    id, track, family, binary, runtime_host = "opencode-api", "B", "opencode", "opencode", "generic"
    weak_model = "deepseek/deepseek-flash"
    implemented = False
    validation = "stub (phase 2)"
    login_hint = "opencode auth login --provider deepseek"

    def auth_check(self, detection: Detection | None = None) -> Auth:
        exe = (detection.path if detection else None) or self._exe()
        code, out = run_probe([exe, "auth", "list"])
        if code == 0 and "deepseek" in out.lower():
            return Auth("ready", "api_key", "deepseek provider configured in opencode")
        return Auth("needs_key", "api_key", "run: " + self.login_hint)


class PiApi(Backend):
    id, track, family, binary, runtime_host = "pi-api", "B", "pi", "pi", "generic"
    weak_model = "deepseek/deepseek-flash"
    implemented = False
    validation = "stub (phase 2)"
    login_hint = "export DEEPSEEK_API_KEY=...   (in your shell profile; Prewalk never stores it)"

    def auth_check(self, detection: Detection | None = None) -> Auth:
        # Presence only; the value is never read into Prewalk state.
        if "DEEPSEEK_API_KEY" in os.environ:
            return Auth("ready", "api_key", "DEEPSEEK_API_KEY is set")
        return Auth("needs_key", "api_key", "run: " + self.login_hint)
