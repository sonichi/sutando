"""Retire a credential file's binding to an agent its owner deleted on the server.

The relay answers a deleted agent's bearer with a plain 401; only the re-enrolment claim's
410 agent_deleted says the agent is gone for good, and a deleted agent's bearer never works
again. retire() moves the lines that hold that bearer, and the lines naming the deleted agent
id, out of the file into a sibling `<file>.retired` (0600), so a host that reads the file sees
this machine as unbound and a new agent can be set up without anyone deleting files by hand.
Which lines those are is gateway_credential_sources.split_deleted_binding's call; this module
only does the file I/O. Stdlib only.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
import time
from typing import List, Optional

from . import gateway_credential_sources

RETIRED_SUFFIX = ".retired"


def _replace(path: str, text: str, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=f".{os.path.basename(path)}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def retire(path: str, *, secret: str, agent_id: str, now: "Optional[float]" = None) -> "List[str]":
    """Move the deleted binding's lines from `path` to `path + RETIRED_SUFFIX`; the keys moved, []
    when the file holds none of them. The sidecar is written first, so a failure part-way loses no
    line. OSError propagates."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    kept, retired, keys = gateway_credential_sources.split_deleted_binding(text, secret=secret, agent_id=agent_id)
    if not retired:
        return []
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))
    header = f"# {stamp} retired: the server deleted {agent_id or 'this agent'}; this credential no longer works\n"
    fd = os.open(path + RETIRED_SUFFIX, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(header + "".join(retired))
    _replace(path, kept, os.stat(path).st_mode & 0o777)
    return keys
