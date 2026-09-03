"""Tri-state tmux session probe shared by every core-liveness reader.

`has_session()` answers True (session present), False (the server answered and
said the session is absent) or None (the probe observed nothing). Absence is
matched POSITIVELY: only the messages a tmux server or a socket lookup emits
for "no such session" / "no server" count as absent. Every other non-zero exit
is None — a missing, hung or signalled binary, and a client the server refused
(a vendored tmux of another version exits 1 with "server exited unexpectedly"
before any session is consulted). A pattern list guards the safe side: a
message this repo has never seen degrades to unobserved, never to death.

Dependency-light on purpose: stdlib only, so the 3-second liveness loop, the
supervisor gate and the heartbeat can all import it without the rest of src/.
"""

import subprocess
from typing import Optional

# The server's own "no such session" answer, and the two forms a dead socket
# takes; a connect failure counts only with the definitive "No such file" reason.
ABSENT_SIGNATURES = (
    "can't find session",
    "no server running",
    "(No such file or directory)",
)


def classify(returncode: Optional[int], stderr) -> Optional[bool]:
    """Map a has-session exit to True / False / None (unobserved).

    `stderr` may be bytes, str or None — subprocess doubles in tests hand back
    result objects without one, and a non-zero exit with no recognised absence
    message is unobserved, not absent."""
    if returncode is None:
        return None
    if returncode == 0:
        return True
    if isinstance(stderr, bytes):
        text = stderr.decode("utf-8", "replace")
    else:
        text = stderr or ""
    if any(sig in text for sig in ABSENT_SIGNATURES):
        return False
    return None


def has_session(socket: str, session: str, timeout: float = 10,
                tmux: str = "tmux") -> Optional[bool]:
    """Run `tmux -S <socket> has-session -t <session>` and classify it."""
    try:
        r = subprocess.run([tmux, "-S", socket, "has-session", "-t", session],
                           capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return classify(r.returncode, getattr(r, "stderr", None))
