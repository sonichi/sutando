#!/usr/bin/env python3
"""Single-instance lock helper for Sutando bridges.

Call acquire(name) near the top of each bridge's main() to ensure only
one instance of that bridge runs at a time.  Uses fcntl.flock — advisory
exclusive lock released automatically on process death (SIGTERM/SIGINT/
crash), so no PID-file cleanup is required.

Usage:
    from single_instance import acquire
    acquire("discord-bridge")   # exits cleanly if another instance holds it
"""

import fcntl
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402

# Module-level list keeps the open fd alive for the lifetime of the process.
# Without this, CPython's GC may close the fd and release the lock.
_held_fds: list[int] = []


def acquire(name: str) -> None:
    """Acquire an exclusive non-blocking flock for `name`.

    If another process already holds the lock, print a diagnostic to stderr
    and call os._exit(0) — exit 0 so launchd/supervision does not trigger
    an aggressive restart loop on what is a normal "already running" exit.

    Args:
        name: A short identifier for this bridge (e.g. "discord-bridge").
              Used as the lock filename under <workspace>/state/locks/.
    """
    lock_dir = resolve_workspace() / "state" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{name}.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        os.close(fd)
        print(
            f"[{name}] another instance already holds the lock — exiting cleanly.",
            file=sys.stderr,
            flush=True,
        )
        os._exit(0)
    # Write PID for human-readable diagnostics (not used for locking).
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    _held_fds.append(fd)  # hold reference so GC does not close + release
