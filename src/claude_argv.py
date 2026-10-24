#!/usr/bin/env python3
"""The one reading of which session a claude process's argv names (`--name X` / `--name=X`).

The first `--name` before a bare `--` decides; nothing after `--` (the boot prompt) counts.

Every liveness reader that identifies a core by its argv (the heartbeat, the quota seat
probe, the launch-record probe, the bare-launch settle helper) takes the answer from here.
Token-wise on purpose: a substring test on `--name sutando-core` is also satisfied by
`sutando-core-watcher` and `sutando-core-2`, each a live false-healthy path once.
Dependency-light (stdlib only) so the 3-second loops can import it without the rest of src/.

CLI, for the shell launcher: `claude_argv.py names-session "<args>" <session>` exits 0 when
the argv names exactly that session, 1 otherwise; `session-name "<args>"` prints the name.
"""
from __future__ import annotations

import shlex
import sys


def session_name(args: str) -> str:
    """The value of the FIRST `--name` (next token, or `--name=`) before a bare `--`, else ''.

    The launcher puts the boot prompt after `--`, so prompt text never names a session;
    a `--name` with no value names none.
    """
    try:
        toks = shlex.split(args or "")
    except ValueError:
        toks = (args or "").split()
    for i, tok in enumerate(toks):
        if tok == "--":
            return ""
        if tok == "--name":
            return toks[i + 1] if i + 1 < len(toks) else ""
        if tok.startswith("--name="):
            return tok[len("--name="):]
    return ""


def names_session(args: str, session: str) -> bool:
    """True only when argv names exactly `session`."""
    return bool(session) and session_name(args) == session


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "names-session":
        return 0 if names_session(argv[1], argv[2]) else 1
    if len(argv) == 2 and argv[0] == "session-name":
        print(session_name(argv[1]))
        return 0
    print("usage: claude_argv.py names-session <args> <session> | session-name <args>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
