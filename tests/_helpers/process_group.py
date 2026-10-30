"""Fixture processes that must not outlive their test: start them in their own
process group, observe the group by PGID, and tear the whole group down.

A watcher-shaped fixture keeps a shell resident with `sleep` as its child, so a
kill of the shell alone orphans the sleep; the group is the unit that is owned.

A teardown runs only when the test gets to run it. `bind_to_test` covers the
rest: a watchdog holding the read end of a pipe kills every still-bound group
when this test process exits, however it exits (return, SIGTERM, SIGKILL).
Fixtures started indirectly, whose group the test never learns, embed
`OWNER_LIFELINE_PY` and exit once their owner (`owner_env`: pid and start time) is gone.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

# The trailing no-op stops the shell exec'ing `sleep` in place, which would turn
# the pid's argv into ["sleep", "30"] while a gate may still re-read it.
STABLE_WATCHER_BODY = "#!/bin/sh\nsleep 30; :\n"


def process_exited(pid: int) -> bool:
    """Execution ended; the owning parent may still need to reap a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return state.returncode == 0 and state.stdout.strip().startswith("Z")


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


# Runs in its own session so a signal to the test's group spares it; a group
# whose leader pid now has another start time was reused and is left alone.
_WATCHDOG = r"""
import os, signal, subprocess, sys
groups = {}
for line in sys.stdin:
    op, _, rest = line.rstrip("\n").partition(" ")
    pgid, _, lstart = rest.partition(" ")
    if op == "+":
        groups[int(pgid)] = lstart
    elif op == "-":
        groups.pop(int(pgid), None)
for pgid, lstart in groups.items():
    now = subprocess.run(["ps", "-o", "lstart=", "-p", str(pgid)], capture_output=True, text=True,
                         env=dict(os.environ, LC_ALL="C")).stdout
    if now.strip() and " ".join(now.split()) != lstart:
        continue
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
"""

_watchdog: "subprocess.Popen | None" = None


def start_time(pid: int) -> str:
    """`ps -o lstart=` under the C locale, whitespace-normalized; "" when the pid is gone."""
    out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                         env=dict(os.environ, LC_ALL="C")).stdout
    return " ".join(out.split())


def _send(line: str) -> None:
    global _watchdog
    if _watchdog is None or _watchdog.poll() is not None:
        _watchdog = subprocess.Popen([sys.executable, "-c", _WATCHDOG], stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     text=True, start_new_session=True)
    _watchdog.stdin.write(line + "\n")
    _watchdog.stdin.flush()


def bind_to_test(pgid: int) -> int:
    """SIGKILL group `pgid` when this process exits unless `unbind` ran first.
    The leader's start time is recorded so a recycled pgid is never signalled."""
    _send(f"+ {pgid} {start_time(pgid)}")
    return pgid


def unbind(pgid: int) -> None:
    _send(f"- {pgid}")


def popen_bound(argv, **kw) -> subprocess.Popen:
    """A direct child in its own session, killed with its group if this test dies."""
    p = subprocess.Popen(argv, start_new_session=True, **kw)
    bind_to_test(p.pid)
    return p


# Embedded in a Python fixture that the test starts only indirectly.
# The system ps by path: a fixture may run under a test's PATH with a stubbed ps.
OWNER_LIFELINE_PY = """
import os as _os, subprocess as _sp, threading as _threading, time as _time
def _fixture_lifeline(owner=int(_os.environ.get("FIXTURE_OWNER_PID") or 0),
                      start=_os.environ.get("FIXTURE_OWNER_START", "")):
    while owner:
        _time.sleep(0.5)
        try:
            _os.kill(owner, 0)
        except OSError:
            _os._exit(0)
        if start:
            ps = _sp.run(["/bin/ps", "-o", "lstart=", "-p", str(owner)], capture_output=True, text=True,
                         env=dict(_os.environ, LC_ALL="C"))
            if ps.returncode == 0 and " ".join(ps.stdout.split()) != start:
                _os._exit(0)
_threading.Thread(target=_fixture_lifeline, daemon=True).start()
"""


def owner_env(pid: "int | None" = None) -> dict:
    """The environment that ties an OWNER_LIFELINE_PY fixture to `pid` (default: this process),
    by pid and start time, so a recycled owner pid does not keep the fixture alive."""
    pid = os.getpid() if pid is None else pid
    return {"FIXTURE_OWNER_PID": str(pid), "FIXTURE_OWNER_START": start_time(pid)}
