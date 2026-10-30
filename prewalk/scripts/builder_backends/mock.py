"""mock: hidden test backend.  Runs mock_builder.py with the current Python.

Model selects the behaviour: mock-ok (default), mock-rate-limit, mock-fail,
mock-slow, mock-bad-return.  Used by unit tests and `prewalk test --backend mock`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .base import Auth, Backend, BackendError, Detection, Launch

SCRIPT = Path(__file__).resolve().with_name("mock_builder.py")
MODELS = ("mock-ok", "mock-rate-limit", "mock-fail", "mock-slow", "mock-bad-return")


class MockBackend(Backend):
    id, track, family, binary, runtime_host = "mock", "-", "mock", "", "generic"
    weak_model, weak_effort = "mock-ok", "medium"
    alt_models = MODELS[1:]
    blocked_models = ("mock-expensive",)
    supported_efforts = ("low", "medium", "high")
    listed = False
    in_default_chain = False
    validation = "test double"

    def detect(self) -> Detection:
        return Detection(True, str(SCRIPT), "mock 1.0.0")

    def auth_check(self, detection: Detection | None = None) -> Auth:
        if os.environ.get("PREWALK_MOCK_AUTH") == "logged_out":
            return Auth("needs_login", "unknown", "mock logged out")
        return Auth("ready", "subscription", "mock")

    def launch(self, *, turn, workdir, brief_path, model=None, effort=None, session=None,
               writable_dirs=(), allowed_bash=()) -> Launch:
        model = self.check_model(model)
        if model not in MODELS:
            raise BackendError(f"mock: unknown model {model!r}")
        requested = effort or self.weak_effort
        eff = self.resolve_effort(requested, model)
        argv = [sys.executable, str(SCRIPT), "--model", model, "--turn", str(turn)]
        if session:
            argv += ["--session", session]
        if eff:
            argv += ["--effort", eff]
        return Launch(argv, cwd=str(workdir), stdin_path=str(brief_path), model=model,
                      env_remove=("ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
                      requested_effort=requested, effective_effort=eff, session=session)

    def observed_models(self, events_path: Path) -> list[str]:
        out = []
        try:
            for line in Path(events_path).read_text(encoding="utf-8").splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ev.get("type") == "model":
                    out.append(str(ev.get("model")))
        except OSError:
            pass
        return sorted(set(out))
