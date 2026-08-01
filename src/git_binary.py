"""Resolve a git executable that will actually run.

On macOS `/usr/bin/git` is the Xcode Command Line Tools *shim*, not git: the
file exists whether or not the tools are installed, and invoking it when they
are not pops the system "install command line developer tools" dialog and
returns nothing. (It is one inode hardlinked across `git`, `python3`, `swiftc`,
`clang`, `gcc`, and `make` — same stub for all of them.)

That makes both of the obvious probes wrong:

  * `os.path.exists("/usr/bin/git")` — true on every Mac, proves nothing.
  * `shutil.which("git")` — resolves to the shim on a clean Mac, so a caller
    that trusts it still triggers the dialog the moment it runs the result.

Hardcoding `/usr/bin/git` is worse still: it pins the shim, so a user who
installs a real git (Homebrew, the git-scm.com installer, a static build) keeps
getting the dialog because PATH never gets a say.

Resolution order:

  1. Any git on PATH that is not the system shim — Homebrew, standalone
     installers, anything the user actually put there.
  2. The system git, but only when `xcode-select -p` reports an installed
     developer directory. That is the one probe that does NOT trigger the
     dialog (`/usr/bin/xcode-select` is a real binary, not a stub), and it is
     already the pattern `src/migrate.sh` uses.
  3. None — the caller degrades rather than prompting the user.

Non-Darwin platforms have no shim, so PATH resolution is used directly.

Callers are expected to treat `None` as "git is unavailable" and degrade. Every
current caller uses git for optional provenance (recent-commit lists, stale-
binary cross-checks), so degrading is correct — none of them should be able to
raise a modal system dialog on a machine that never asked for developer tools.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from typing import Callable, Optional

# The Xcode-CLT shim path. Never invoke this without confirming the tools are
# actually installed.
SYSTEM_GIT = "/usr/bin/git"


def developer_tools_installed(run: Callable = subprocess.run) -> bool:
    """True when `xcode-select -p` reports an installed developer directory.

    `run` is injected so tests can exercise both outcomes without depending on
    the host's toolchain state. Any failure to probe is treated as "not
    installed" — failing closed here means we skip git, which is the safe
    direction: the alternative is a modal dialog.
    """
    try:
        proc = run(["xcode-select", "-p"], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def select_git(
    found: Optional[str],
    *,
    is_darwin: bool,
    clt_installed: Callable[[], bool],
) -> Optional[str]:
    """Pure selection step, given whatever PATH resolution returned.

    Split out from `resolve_git` so the ordering is testable without touching
    the host's real PATH or toolchain (same rationale as `selectFfprobe` in
    `src/recording-tools.ts`, PR #2370).

    `clt_installed` is a callable rather than a bool so the `xcode-select`
    probe is only spawned when it can change the answer — i.e. never on a host
    that already has a non-shim git.
    """
    if not found:
        return None
    if not is_darwin:
        return found
    if os.path.realpath(found) != SYSTEM_GIT:
        return found
    return found if clt_installed() else None


@lru_cache(maxsize=1)
def resolve_git() -> Optional[str]:
    """Return a runnable git executable, or None if there isn't one.

    Cached: the answer cannot change within a process lifetime without the user
    installing a toolchain mid-run, and the callers are on polling paths
    (health-check runs on a timer) where re-probing every pass is pure waste.
    """
    return select_git(
        shutil.which("git"),
        is_darwin=sys.platform == "darwin",
        clt_installed=developer_tools_installed,
    )
