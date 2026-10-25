"""The user service manager's commands, for every adapter that supervises a job: one bounded runner
(the seam tests patch), the one helper that names systemctl, and the one reachability probe. systemctl
runs only with a literal argument tuple plus at most a unit name checked against UNIT_RE, so no caller
can make it print the manager's environment. Stdlib only; names no adapter.
"""
from __future__ import annotations

import re
import shutil
import subprocess

TIMEOUT_S = 30
UNIT_RE = re.compile(r"ag2-sparrow(-[a-z0-9-]+)?\.service")


def _run(cmd: list, timeout: float = TIMEOUT_S) -> "subprocess.CompletedProcess | None":
    """A manager command; None when it could not run or did not return in time. Never prompts."""
    try:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _systemd_run(cmd: list, timeout: float = TIMEOUT_S) -> "subprocess.CompletedProcess | None":
    """A systemctl/loginctl command with --no-ask-password: over SSH a polkit password prompt would
    hang until the timeout."""
    return _run([cmd[0], "--no-ask-password", *cmd[1:]], timeout)


def _ok(r) -> bool:
    return r is not None and r.returncode == 0


def _systemctl(args: "tuple[str, ...]", *, locate: bool = False, unit: "str | None" = None,
               timeout: float = TIMEOUT_S):
    """The one place systemctl is named: run with `args`, a literal tuple at every call site, then
    `unit` when given (ValueError unless it matches UNIT_RE); or with locate=True only its path on
    PATH (None when absent)."""
    if locate:
        return shutil.which("systemctl")
    if unit is not None and not (isinstance(unit, str) and UNIT_RE.fullmatch(unit)):
        raise ValueError(f"not an ag2-sparrow unit name: {unit!r}")
    return _systemd_run(["systemctl", *args, *(() if unit is None else (unit,))], timeout)


def systemd_user_available() -> bool:
    """Whether `systemctl --user` reaches a running user manager; a host without systemctl cannot
    run it, so the probe answers False there too."""
    return _ok(_systemctl(("--user", "show-environment")))
