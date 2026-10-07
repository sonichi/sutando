"""Runtime a tmux core session was launched with, read from the session's own environment.

Every launcher stamps `SUTANDO_CORE_RUNTIME` into the session it creates, so this is the
running core's runtime, not the configured one (they disagree mid-switch). `read()` returns
the stamped value or None when the session cannot answer; it never guesses a default, so
each caller keeps its own policy for "unknown". Transport stays with the caller: `read`
takes the caller's tmux runner. Stdlib only, so every liveness reader can import it.
"""

from typing import Callable, Optional

VAR = "SUTANDO_CORE_RUNTIME"


def argv(session: str) -> list:
    """tmux arguments (after `-S <socket>`) that print the session's stamp."""
    return ["show-environment", "-t", f"={session}", VAR]


def parse(returncode: Optional[int], stdout) -> Optional[str]:
    """The stamped runtime from one `show-environment` exit, else None.

    A non-zero or missing exit, an unset variable (`-SUTANDO_CORE_RUNTIME`) and an empty
    value are all None."""
    if returncode != 0:
        return None
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    line = (stdout or "").strip()
    if not line.startswith(VAR + "="):
        return None
    return line.split("=", 1)[1].strip() or None


def read(session: str, run: Callable) -> Optional[str]:
    """`run(*tmux_args)` returns a CompletedProcess-like result, or None when tmux could not run."""
    res = run(*argv(session))
    if res is None:
        return None
    return parse(res.returncode, res.stdout)
