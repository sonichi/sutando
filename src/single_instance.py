"""Single-instance guard for long-running bridge daemons.

Prevents two copies of the same bridge running simultaneously — a real
failure mode when startup.sh and Sutando.app's launchd agent race at boot
(observed 2026-05-xx: duplicate telegram-bridge caused API 409 errors;
duplicate discord-bridge caused double task delivery).

Usage (at the top of a bridge's main entry point):

    from single_instance import acquire
    acquire("telegram-bridge")   # or "discord-bridge" / "slack-bridge"

The lock is an OS-level exclusive non-blocking lock on a file in
`$WORKSPACE/state/locks/<name>.lock` — `fcntl.flock` on POSIX, the
equivalent `msvcrt.locking` byte-range lock on Windows. It auto-releases
on process death (OS closes all FDs), so no explicit cleanup is needed.
The FD is kept in `_held_fds` to prevent CPython's GC from closing it early.

Exit behavior: exits EXIT_STANDDOWN (75) on lock contention. NOT 0 -- a
supervisor cannot distinguish an inferred stand-down from a bridge whose
main loop merely returned, and treating the latter as deliberate leaves it
silently down. 75 cannot be reached by falling off __main__.
Historically 0, to stop launchd's KeepAlive
doesn't restart-loop when it's simply the second instance.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from file_lock import lock_fd  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

# Distinct from 0 so a supervisor can tell a deliberate stand-down from a main
# loop that simply returned. 75 == EX_TEMPFAIL.
EXIT_STANDDOWN = 75

_held_fds: list[int] = []  # keep refs so GC doesn't close them


def acquire(name: str) -> None:
    """Acquire an exclusive non-blocking lock for `name`.

    If another process already holds the lock, prints a one-line message
    to stderr and exits EXIT_STANDDOWN. Otherwise returns; caller continues.
    """
    lock_dir = resolve_workspace() / "state" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{name}.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)

    try:
        lock_fd(fd, blocking=False)
    except (BlockingIOError, OSError):
        _exit_contended(fd, name)

    # Overwrite PID so tooling can inspect who holds the lock (offset 0,
    # outside the Windows lock region).
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _held_fds.append(fd)  # lock auto-releases on process exit (OS closes FD)


def _exit_contended(fd: int, name: str) -> None:
    os.close(fd)
    print(
        f"[{name}] another instance already holds the lock — exiting cleanly.",
        file=sys.stderr, flush=True,
    )
    os._exit(EXIT_STANDDOWN)
