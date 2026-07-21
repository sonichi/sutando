#!/usr/bin/env python3
"""
Tests for `check_task_watcher` — direct liveness of the streaming task watcher.

Motivated by 2026-07-21: the watcher was dead, tasks/ was empty, and
health-check reported 0 failures. Neither existing consequence check can see
that state — `check_task_queue` needs >3 tasks AND >300s age (a single
stranded owner DM never trips the count), and `check_core_proactive_loop`
reads core-status.json, which is freshest precisely when the loop is alive
and the watcher is not.

Covers:
  a) no core alive → ok (watcher not expected; must not latch red on hosts
     that simply aren't running Sutando)
  b) core alive, sentinel absent → warn
  c) core alive, sentinel holds a dead PID → warn (crashed, sentinel left behind)
  d) core alive, PID alive but argv is not the watcher → warn (PID reuse)
  e) core alive, PID alive and argv names the watcher → ok
  f) core alive, sentinel unparseable → warn (not a crash)
  g) the check is registered in run_checks' output
  h) _proc_argv against real PIDs (live + nonexistent) — the OS-facing half
  i) _proc_argv swallows a probe failure rather than failing the health check

Run: python3 tests/health-check-task-watcher.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


def make_workspace(td: Path, *, core_alive: bool, pid_text: str | None) -> Path:
    """Build a temp workspace. `core_alive` stamps a fresh heartbeat file;
    `pid_text=None` means no sentinel at all."""
    state = td / "state"
    state.mkdir(parents=True, exist_ok=True)
    if core_alive:
        cores = state / "cores"
        cores.mkdir(exist_ok=True)
        beat = cores / "testhost.alive"
        beat.write_text("{}")
        # _any_core_alive uses a 90s window; a just-written file is inside it.
    if pid_text is not None:
        (state / "watch-tasks-stream.pid").write_text(pid_text)
    return td


def run_check(*, core_alive: bool, pid_text: str | None, argv: str | None = None) -> dict:
    """Call check_task_watcher against a temp WORKSPACE_DIR. `argv` patches
    the _proc_argv probe: None = leave the real one (only used where no PID
    is read), "" = process gone, any string = that process's argv."""
    with tempfile.TemporaryDirectory() as td:
        make_workspace(Path(td), core_alive=core_alive, pid_text=pid_text)
        orig_ws, orig_probe = hc.WORKSPACE_DIR, hc._proc_argv
        try:
            hc.WORKSPACE_DIR = Path(td)
            if argv is not None:
                hc._proc_argv = lambda pid: argv
            return hc.check_task_watcher()
        finally:
            hc.WORKSPACE_DIR, hc._proc_argv = orig_ws, orig_probe


def case_a_no_core_is_ok() -> list[str]:
    # The anti-latch guard: a host with no core running must not sit red.
    r = run_check(core_alive=False, pid_text=None)
    if r["status"] != "ok":
        return [f"a) no core alive should be ok, got {r['status']} ({r['detail']})"]
    return []


def case_b_sentinel_absent_warns() -> list[str]:
    r = run_check(core_alive=True, pid_text=None)
    if r["status"] != "warn":
        return [f"b) core alive + no sentinel should warn, got {r['status']}"]
    return []


def case_c_dead_pid_warns() -> list[str]:
    r = run_check(core_alive=True, pid_text="424242", argv="")
    if r["status"] != "warn":
        return [f"c) dead watcher PID should warn, got {r['status']}"]
    if "dead" not in r["detail"]:
        return [f"c) detail should name the crash, got {r['detail']!r}"]
    return []


def case_d_pid_reuse_warns() -> list[str]:
    # kill -0 alone would call this alive — the argv check is what catches it.
    r = run_check(core_alive=True, pid_text="4242", argv="/usr/sbin/cupsd -l")
    if r["status"] != "warn":
        return [f"d) PID reuse should warn, got {r['status']}"]
    if "reuse" not in r["detail"]:
        return [f"d) detail should name PID reuse, got {r['detail']!r}"]
    return []


def case_e_live_watcher_is_ok() -> list[str]:
    r = run_check(core_alive=True, pid_text="4242", argv="bash src/watch-tasks-stream.sh")
    if r["status"] != "ok":
        return [f"e) live watcher should be ok, got {r['status']} ({r['detail']})"]
    return []


def case_f_unparseable_sentinel_warns() -> list[str]:
    r = run_check(core_alive=True, pid_text="not-a-pid", argv="")
    if r["status"] != "warn":
        return [f"f) unparseable sentinel should warn, got {r['status']}"]
    if "dead" in r["detail"]:
        return ["f) an unreadable sentinel is not a crash — detail should not say 'dead'"]
    return []


def case_g_registered_in_run_checks() -> list[str]:
    """A check nobody calls is not a check. Guards the registration line.

    Match the full `checks.append(...)` call, NOT the bare `check_task_watcher()`:
    that shorter string is a substring of the function's own `def` line, so it
    matches whether or not the check is ever registered — the first version of
    this case was vacuous for exactly that reason (caught by deleting the
    registration and watching the suite stay green).
    """
    src = (REPO / "src" / "health-check.py").read_text()
    if "checks.append(check_task_watcher())" not in src:
        return ["g) check_task_watcher() is never appended to the checks list"]
    return []


def case_h_proc_argv_reads_a_real_process() -> list[str]:
    """Exercise the real probe, not the stub the cases above patch in.

    This is the half that talks to the OS, so it needs to run against actual
    PIDs or nothing verifies that `ps -p <pid> -o args=` returns what the
    caller expects.
    """
    fails = []
    mine = hc._proc_argv(os.getpid())
    if not mine:
        fails.append("h) _proc_argv(os.getpid()) returned empty for a live process")
    elif "python" not in mine.lower():
        fails.append(f"h) argv for this process should name the interpreter, got {mine[:60]!r}")
    # A PID that cannot be running: above the platform maximum.
    gone = hc._proc_argv(4_000_000)
    if gone != "":
        fails.append(f"h) a nonexistent PID should give '', got {gone[:40]!r}")
    return fails


def case_i_proc_argv_swallows_probe_failure() -> list[str]:
    """A broken/absent `ps` must not take the health check down with it —
    the probe degrades to 'no argv', which the caller reads as 'not running'."""
    orig = hc.subprocess.run
    try:
        hc.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(OSError("ps missing"))
        got = hc._proc_argv(1)
    finally:
        hc.subprocess.run = orig
    if got != "":
        return [f"i) a raising probe should return '', got {got!r}"]
    return []


def main() -> int:
    cases = [
        ("a", case_a_no_core_is_ok),
        ("b", case_b_sentinel_absent_warns),
        ("c", case_c_dead_pid_warns),
        ("d", case_d_pid_reuse_warns),
        ("e", case_e_live_watcher_is_ok),
        ("f", case_f_unparseable_sentinel_warns),
        ("g", case_g_registered_in_run_checks),
        ("h", case_h_proc_argv_reads_a_real_process),
        ("i", case_i_proc_argv_swallows_probe_failure),
    ]
    all_failures = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as e:
            fails = [f"{label}) raised {type(e).__name__}: {e}"]
        if fails:
            all_failures.extend(fails)
            print(f"  ✗ case {label}")
            for f in fails:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    print("\nTask-watcher liveness invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
