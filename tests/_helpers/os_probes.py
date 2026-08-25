"""Preconditions for tests that deliberately exercise a REAL OS probe.

A live-`ps` self-probe is the only thing verifying that this repo's parsing
works on the host OS, so it must not be stubbed away. But it must also not
FAIL where `ps` is unavailable — sandboxed reviewers and minimal CI images
have no process visibility, and reporting a defect that is not there costs a
review round trip every time (observed on sonichi/sutando#3328).

Skip, loudly, with a reason. A silent skip is worse than a failure.
"""

from __future__ import annotations

import os
import subprocess

PS_SKIP_REASON = "`ps` is unavailable here, so the live-OS probe cannot run"


def ps_available() -> bool:
    """True when `ps` runs AND returns a table naming this process.

    Deliberately stronger than "the binary exists": a `ps` that runs but shows
    no processes (some containers) would let a live probe assert on an empty
    table, which passes for the wrong reason.
    """
    try:
        r = subprocess.run(
            ["ps", "-o", "pid=,ppid=", "-p", str(os.getpid())],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and str(os.getpid()) in (r.stdout or "")
