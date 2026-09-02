"""Tri-state tmux session probe shared by every core-liveness reader.

`has_session()` answers True (session present), False (the server answered and
the session is absent) or None (the probe could not observe). Two things are
None, not False: a tmux binary that is missing, hung or refuses to exec, and a
client that never reached the session lookup because it could not talk to the
server at all. The second case is a client/server version skew — a vendored
tmux of one version probing a server of another exits 1 with "server exited
unexpectedly" before any session is consulted, which a bare `returncode != 0`
reads as a dead core. Only a server that answered and said no is absent.

Dependency-light on purpose: stdlib only, so the 3-second liveness loop and the
supervisor gate can both import it without pulling in the rest of src/.
"""

import subprocess
from typing import Optional

# stderr the tmux CLIENT prints when it never got a session answer: the server
# rejected the client (version skew) or the connection collapsed mid-request.
CLIENT_FAULT_SIGNATURES = (
    "server exited unexpectedly",
    "protocol version mismatch",
    "lost server",
)


def classify(returncode: Optional[int], stderr) -> Optional[bool]:
    """Map a has-session exit to True / False / None (unobserved).

    `stderr` may be bytes, str or None — subprocess doubles in tests hand back
    result objects without one, and that must read as an ordinary exit."""
    if returncode is None:
        return None
    if returncode == 0:
        return True
    if isinstance(stderr, bytes):
        text = stderr.decode("utf-8", "replace")
    else:
        text = stderr or ""
    if any(sig in text for sig in CLIENT_FAULT_SIGNATURES):
        return None
    return False


def has_session(socket: str, session: str, timeout: float = 10,
                tmux: str = "tmux") -> Optional[bool]:
    """Run `tmux -S <socket> has-session -t <session>` and classify it."""
    try:
        r = subprocess.run([tmux, "-S", socket, "has-session", "-t", session],
                           capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return classify(r.returncode, getattr(r, "stderr", None))
