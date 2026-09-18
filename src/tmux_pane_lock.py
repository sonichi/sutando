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

REPO_ROOT = Path(__file__).resolve().parent.parent
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
        deadline = time.time() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                if time.time() >= deadline:
                    break
                time.sleep(0.05)
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
