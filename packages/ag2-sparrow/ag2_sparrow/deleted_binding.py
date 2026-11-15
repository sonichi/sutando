"""Retire a credential file's binding to an agent its owner deleted on the server.

The relay answers a deleted agent's bearer with a plain 401; only the re-enrolment claim's
410 agent_deleted says the agent is gone for good, and a deleted agent's bearer never works
again. retire() moves the lines that hold that bearer, and the lines naming the deleted agent
id, out of the file into a sibling `<file>.retired` (0600), so a host that reads the file sees
this machine as unbound and a new agent can be set up without anyone deleting files by hand.
Which lines those are, and whether the file ties them to the deleted id at all, is
gateway_credential_sources.split_deleted_binding's call; this module only does the file I/O.

Writer contract: the file is replaced atomically and only if it is byte-identical to what was
read (another program, the desktop's connect, also writes it); a symlink, an unreadable or
non-UTF-8 file, or a file that changed is left untouched and reported as nothing moved. Line
endings are kept. Stdlib only.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
import time
from typing import List, Optional

from . import gateway_credential_sources

RETIRED_SUFFIX = ".retired"


def _read(path: str) -> "Optional[tuple[str, os.stat_result]]":
    """(text, stat) of a regular, non-link UTF-8 file; None otherwise."""
    if os.path.islink(path):
        return None
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            st = os.fstat(fh.fileno())
            return fh.read(), st
    except (OSError, ValueError):
        return None


def _same(path: str, text: str, st: os.stat_result) -> bool:
    now = _read(path)
    return now is not None and now[0] == text and (now[1].st_ino, now[1].st_size) == (st.st_ino, st.st_size)


def _append_sidecar(path: str, lines: str) -> None:
    fd = os.open(path + RETIRED_SUFFIX, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8", newline="") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(lines)


def retire(path: str, *, secret: str, agent_id: str, now: "Optional[float]" = None) -> "List[str]":
    """Move the deleted binding's lines from `path` to `path + RETIRED_SUFFIX`; the keys moved, []
    when nothing moved (see the writer contract above). The sidecar is written before the file is
    replaced, so a failure part-way loses no line. An OSError writing propagates."""
    got = _read(path)
    if got is None:
        return []
    text, st = got
    kept, retired, keys = gateway_credential_sources.split_deleted_binding(text, secret=secret, agent_id=agent_id)
    if not retired:
        return []
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))
    header = (f"# {stamp} retired: the server deleted {agent_id}; this credential no longer works. "
              f"To undo, copy the lines below back into {os.path.basename(path)}.\n")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=f".{os.path.basename(path)}.")
    try:
        os.fchmod(fd, st.st_mode & 0o777)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(kept)
        if not _same(path, text, st):
            os.unlink(tmp)
            return []
        _append_sidecar(path, header + "".join(retired))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return keys
