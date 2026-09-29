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
import shutil
import subprocess
import sys

PS_SKIP_REASON = "`ps` is unavailable here, so the live-OS probe cannot run"
SWIFTC_SKIP_REASON = (
    "the Swift probes build macOS-targeted sources, so they run only on macOS "
    "with a real toolchain"
)


def swiftc_usable() -> bool:
    """True only where `swiftc` can build this repo's macOS-targeted Swift.

    Two different hosts answer "is swiftc here?" misleadingly, and each cost a
    CI incident:

    * **Linux.** The sources under `src/Sutando/` target macOS. A Linux image
      that ships a Swift toolchain makes a which-only guard run them there for
      the first time, and every test errors in `setUp` with a bare exit status
      (observed 2026-09-22 on ubuntu24 image 20260920.314; the compiler's own
      message is discarded by those harnesses, so the log names no cause).
    * **macOS without the CLT.** `/usr/bin/swiftc` is the Command Line Tools
      stub and exists with no toolchain installed, so invoking it raises the
      install dialog the probes exist to avoid. `xcode-select -p` answers
      without prompting.
    """
    if sys.platform != "darwin":
        return False
    if not shutil.which("swiftc"):
        return False
    try:
        return subprocess.run(
            ["xcode-select", "-p"], capture_output=True, timeout=10
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


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
