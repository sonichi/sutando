#!/usr/bin/env python3
"""A watcher must only delete the PID sentinel it still OWNS.

THE DEFECT (reproduced live 2026-08-04). `cleanup()` ran `rm -f "$PID_FILE"`
unconditionally on EXIT. The sentinel records ONE pid and a second watcher
overwrites it at startup, so when the FIRST watcher exits — which is exactly
what stopping a stale duplicate looks like — it deletes a sentinel that now
names the LIVE watcher. Everything downstream then reads a healthy watcher as
dead, and the documented recovery spawns another one:

    health-check `task-watcher` -> "orphaned watcher(s) ... with no PID sentinel"
    /proactive-loop step 9      -> sentinel missing => start one  -> duplicate
    /schedule-crons step 5      -> same PID-check, same conclusion

So stopping a duplicate manufactures the next duplicate. Observed on this host:
stopping one stale watcher deleted the live watcher's sentinel, health reported
the live one as orphaned within seconds, and following the documented recovery
would have produced a third.

WHY THIS IS A PYTHON TEST DRIVING REAL PROCESSES, and not a shell test or a grep:

  * A grep cannot see it. The bug lives in the interaction between two processes
    and one shared file; the buggy line is individually reasonable.
  * The watcher does not service SIGTERM promptly — it blocks reading from
    fswatch, and bash defers the trap until that read returns. So a test cannot
    drive `cleanup()` with a signal to the watcher. It CAN by killing the
    watcher's fswatch child: the read hits EOF and the script exits through its
    EXIT trap, which is the path that runs cleanup.
  * `cleanup()` ends in `kill 0`, which signals the whole PROCESS GROUP. A
    harness that starts the watcher in its own group gets killed by the code it
    is testing (this cost me a shell before `start_new_session=True` went in).
    Every watcher here is started in its OWN session for that reason.

CLEANUP DISCIPLINE: every kill is by a pid this test recorded, via killpg on a
session this test created. Never `pkill -f watch-tasks-stream` — that pattern
matches the operator's own live watcher, and using it during manual testing is
what took the core's watcher down on 2026-08-04.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class Watcher:
    """One watcher in its OWN session, so its `kill 0` cannot reach us."""

    def __init__(self, workspace: Path):
        env = dict(os.environ, SUTANDO_WORKSPACE=str(workspace), SUTANDO_TEST_MODE="1")
        self.proc = subprocess.Popen(
            ["bash", "src/watch-tasks-stream.sh"], cwd=str(REPO), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def retire_via_eof(self) -> None:
        """Exit through the EXIT trap — the only path that runs cleanup().

        Kills the fswatch CHILD so the script's read returns EOF. A SIGTERM to
        the script itself is deferred while it blocks on that read.
        """
        kids = subprocess.run(["pgrep", "-P", str(self.pid)],
                              capture_output=True, text=True).stdout.split()
        for k in kids:
            try:
                os.kill(int(k), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass

    def hard_stop(self) -> None:
        try:
            os.killpg(os.getpgid(self.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def wait_for(pred, timeout: float = 8.0, step: float = 0.25) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def main() -> int:
    print("watch-tasks-stream sentinel ownership:")
    box = Path(tempfile.mkdtemp(prefix="sentinel-own-"))
    ws = box / "workspace"
    for d in ("tasks", "results", "state"):
        (ws / d).mkdir(parents=True, exist_ok=True)
    sentinel = ws / "state" / "watch-tasks-stream.pid"
    a = b = None
    try:
        a = Watcher(ws)
        check("a watcher writes the sentinel",
              wait_for(lambda: sentinel.exists() and sentinel.read_text().strip() != ""),
              "sentinel never appeared")
        pid_a = sentinel.read_text().strip() if sentinel.exists() else ""

        b = Watcher(ws)
        check("a SECOND watcher takes ownership of the sentinel",
              wait_for(lambda: sentinel.exists() and sentinel.read_text().strip() not in ("", pid_a)),
              f"sentinel still reads {pid_a!r}")
        pid_b = sentinel.read_text().strip() if sentinel.exists() else ""

        # THE REGRESSION: retire the STALE watcher; the live one must keep its file.
        a.retire_via_eof()
        exited = wait_for(lambda: not a.alive(), timeout=10)
        check("the stale watcher exits through its EXIT trap (so cleanup RUNS)",
              exited,
              "it never exited — cleanup did not run, so the assertion below "
              "would pass vacuously")

        survived = sentinel.exists() and sentinel.read_text().strip() == pid_b
        check("stopping the STALE watcher leaves the LIVE sentinel intact",
              survived,
              f"sentinel={sentinel.read_text().strip() if sentinel.exists() else '<DELETED>'} "
              f"expected={pid_b} — a dying duplicate erased a live watcher's pid file")
        check("  ...and the live watcher really is still running",
              b.alive(),
              "it died too, so the check above proved nothing")

        # The mirror: a watcher that DOES own the file must still release it,
        # otherwise the guard is satisfiable by never deleting anything and a
        # stale sentinel would outlive every watcher.
        b.retire_via_eof()
        gone = wait_for(lambda: not b.alive(), timeout=10)
        check("the owning watcher exits through its EXIT trap", gone, "never exited")
        check("  ...and REMOVES the sentinel it owns",
              gone and not sentinel.exists(),
              f"left {sentinel.read_text().strip() if sentinel.exists() else ''!r} behind — "
              f"a stale sentinel makes every later health probe lie")
    finally:
        for w in (a, b):
            if w is not None:
                w.hard_stop()
        shutil.rmtree(box, ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All sentinel-ownership checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
