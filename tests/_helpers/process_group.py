"""Fixture processes that must not outlive their test: start them in their own
process group, observe the group by PGID, and tear the whole group down.

A watcher-shaped fixture keeps a shell resident with `sleep` as its child, so a
kill of the shell alone orphans the sleep; the group is the unit that is owned.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

# The trailing no-op stops the shell exec'ing `sleep` in place, which would turn
# the pid's argv into ["sleep", "30"] while a gate may still re-read it.
STABLE_WATCHER_BODY = "#!/bin/sh\nsleep 30; :\n"


def group_members(pgid: int) -> list[int]:
    """Live pids whose process group is pgid, from an all-process listing: a
    numeric `ps -g` means a session on procps, so the PGID column is filtered
    here. A zombie awaiting wait() is not a leak. Raises if ps gave no table."""
    r = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True)
    rows = [line.split() for line in r.stdout.splitlines() if line.strip()]
    if r.returncode != 0 or not rows:
        raise RuntimeError(f"ps gave no process table (rc={r.returncode}): {r.stderr.strip()}")
    return [int(pid) for pid, pg, stat, *_ in rows
            if pg == str(pgid) and not stat.startswith("Z")]


def kill_group(pgid: int, wait_s: float = 2.0) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        members = group_members(pgid)
        if not members:
            return
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            for pid in members:   # a group with a reaped leader refuses killpg; signal each member
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline and group_members(pgid):
            time.sleep(0.02)


def spawn_detached(script: str, inbox: str) -> int:
    """Start `bash script inbox` reparented away from the caller, in its own
    process group (pgid == pid), so teardown can take shell and child together."""
    out = subprocess.run(
        ["bash", "-c", f'set -m; nohup bash "{script}" "{inbox}" >/dev/null 2>&1 & echo $!'],
        capture_output=True, text=True, check=True).stdout.strip()
    return int(out)


def popen_in_own_group(argv, **kw) -> subprocess.Popen:
    """A direct child in its own process group (pgid == pid)."""
    kw.setdefault("stdout", subprocess.DEVNULL)
    kw.setdefault("stderr", subprocess.DEVNULL)
    return subprocess.Popen(argv, preexec_fn=os.setpgrp, **kw)


def own_group(testcase, pgid: int, reap=None) -> None:
    """Register teardown on a TestCase: kill the group, reap, then assert nothing live remains."""
    testcase.addCleanup(lambda: testcase.assertEqual(group_members(pgid), [], "fixture left a process behind"))
    if reap is not None:
        testcase.addCleanup(reap)
    testcase.addCleanup(kill_group, pgid)
