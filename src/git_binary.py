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
import subprocess
import sys
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


def path_candidates(
    name: str = "git",
    path_env: Optional[str] = None,
    is_exec: Optional[Callable[[str], bool]] = None,
) -> list:
    """Every executable `name` on PATH, in PATH order.

    `shutil.which` returns only the FIRST match, which is not enough here: if a
    stub-first PATH puts /usr/bin ahead of a real install, `which` hands back
    the stub and a later runnable git is never considered — contradicting this
    module's own stated order (@john-the-dev, reviewing #2469). Service PATHs
    routinely look like that.
    """
    env = os.environ.get("PATH", "") if path_env is None else path_env
    if is_exec is None:
        def is_exec(p: str) -> bool:  # noqa: E306
            return os.path.isfile(p) and os.access(p, os.X_OK)
    out = []
    for directory in env.split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        if is_exec(candidate):
            out.append(candidate)
    return out


def select_git(
    candidates: list,
    *,
    is_darwin: bool,
    clt_installed: Callable[[], bool],
    realpath: Callable[[str], str] = os.path.realpath,
) -> Optional[str]:
    """First runnable non-stub git in PATH order; the stub only as a fallback.

    Walks every candidate rather than judging one: a stub-first PATH must not
    hide a real git further along. The system stub is remembered and returned
    only when the developer tools are installed (which makes it a working git).

    Split out from `resolve_git` so the ordering is testable without touching
    the host's real PATH or toolchain (same rationale as `selectFfprobe` in
    `src/recording-tools.ts`, PR #2370).

    `clt_installed` is a callable rather than a bool so the `xcode-select`
    probe is only spawned when it can change the answer — i.e. never on a host
    where a real git was found first.
    """
    stub = None
    for candidate in candidates:
        if not is_darwin or realpath(candidate) != SYSTEM_GIT:
            return candidate
        if stub is None:
            stub = candidate
    if stub is not None and clt_installed():
        return stub
    return None


class GitUnavailable(FileNotFoundError):
    """Raised by `git_argv` when there is no runnable git.

    Subclasses FileNotFoundError — and therefore OSError — on purpose: every
    current call site already degrades on git failure by catching OSError, so
    an unavailable git flows through the handling they already have instead of
    needing a new branch at each site. That keeps the fix to one changed line
    per caller and puts the whole decision in this module, where it is cheap to
    test.
    """


def git_argv(*args: str) -> list:
    """Build a full argv for a git invocation, or raise `GitUnavailable`.

    Call this INSIDE the caller's existing try-block. Never assemble
    `[resolve_git(), ...]` by hand — `resolve_git()` can return None, and
    `subprocess.run(None)` fails with a TypeError that reads like a bug rather
    than the intended "this host has no git" signal.
    """
    git = resolve_git()
    if git is None:
        raise GitUnavailable(
            "no runnable git on this host; refusing to invoke the Xcode-CLT "
            "shim at " + SYSTEM_GIT
        )
    return [git, *args]


_resolved: Optional[str] = None


def resolve_git() -> Optional[str]:
    """Return a runnable git executable, or None if there isn't one.

    Caches only a POSITIVE answer, on purpose.

    An earlier revision memoised both outcomes, reasoning that the answer cannot
    change within a process lifetime. That holds for `health-check.py`, which is
    re-exec'd on a timer — but not for `agent-api.py`, a long-lived
    `serve_forever()` HTTP server that calls this inside `do_GET`. There, "the
    user installs the developer tools mid-run" is the EXPECTED case, not an
    exotic one: they may well be installing them *because* something told them
    to. A cached None left `GET /activity` permanently empty until someone
    restarted the service, while health-check on the same host reported git as
    fine — the two callers disagreeing, with the never-restarted one wrong.
    (Caught by @sonichi reviewing #2469.)

    A negative therefore stays re-probed. `shutil.which` on a warm filesystem is
    not a cost worth a permanently wrong answer; the `xcode-select` spawn behind
    it only runs when PATH resolves to the stub, which is the already-degraded
    host.
    """
    global _resolved
    if _resolved is not None:
        return _resolved
    _resolved = select_git(
        path_candidates("git"),
        is_darwin=sys.platform == "darwin",
        clt_installed=developer_tools_installed,
    )
    return _resolved


def reset_cache_for_tests() -> None:
    """Drop the memoised positive answer. Tests only."""
    global _resolved
    _resolved = None
