"""Process-group helpers shared by the async supervisor and cancel (POSIX + Windows)."""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any


def popen_group_kwargs() -> dict[str, Any]:
    """Start the Builder in its own process group so cancel reaches its children."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True}


def signal_group(pid: int, name: str) -> None:
    """Deliver one step of a backend's cancel sequence to the Builder group.

    POSIX: killpg(SIGINT|SIGTERM|SIGKILL).  Windows: CTRL_BREAK for the soft
    steps, ``taskkill /T /F`` for the hard step.  A vanished group is not an error.
    """
    if os.name == "nt":  # pragma: no cover
        if name == "SIGKILL":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, check=False)
        else:
            try:
                os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            except OSError:
                pass
        return
    sig = getattr(signal, name, signal.SIGTERM)
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass
