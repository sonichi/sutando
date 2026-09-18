"""Take the per-pane writer lock that every automated pane writer holds.

scripts/tmux-pane-lock.sh stays the single owner of the lock-path derivation, so a
Python writer and a shell writer contend for the same file.
"""

import fcntl
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root — locates the CODE script scripts/tmux-pane-lock.sh
DEFAULT_TIMEOUT = 5.0


def lock_path(sock: str, session: str, repo: Optional[Path] = None) -> Optional[str]:
    """The pane lock for sock+session, or None when it cannot be derived (never a guess)."""
    script = Path(repo or REPO_ROOT) / "scripts" / "tmux-pane-lock.sh"
    try:
        r = subprocess.run(["bash", str(script), sock, session],
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    path = r.stdout.strip()
    return path if r.returncode == 0 and path else None


def flock_fd(fd: int, timeout: Optional[float] = None) -> bool:
    """Take LOCK_EX on an ALREADY-OPEN fd. timeout None waits; a number deadlines.

    The one acquisition for every pane writer: scripts/tmux-pane-lock.bash calls this
    via the CLI below on a caller-chosen fd, so shell and Python cannot drift apart.
    """
    if timeout is None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return True
    deadline = time.time() + max(0.0, timeout)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.time() >= deadline:
                return False
            time.sleep(0.05)


@contextmanager
def pane_lock(sock: str, session: str, timeout: float = DEFAULT_TIMEOUT,
              repo: Optional[Path] = None) -> Iterator[bool]:
    """Yield True while this process owns the pane, False when it does not.

    A False is another writer's transaction in progress: defer, never send anyway.
    """
    path = lock_path(sock, session, repo)
    if path is None:
        yield False
        return
    fd = None
    try:
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    except OSError:
        yield False
        return
    held = False
    try:
        held = flock_fd(fd, timeout)
        yield held
    finally:
        if held:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass


if __name__ == "__main__":
    # Flock an fd this process INHERITED, so a shell transaction keeps the lock after
    # we exit: flock lives on the open file description, which survives across exec.
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--flock-fd", type=int, required=True)
    ap.add_argument("--timeout", default="")
    a = ap.parse_args()
    to = float(a.timeout) if str(a.timeout).strip() else None
    raise SystemExit(0 if flock_fd(a.flock_fd, to) else 1)
