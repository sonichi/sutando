#!/usr/bin/env python3
"""The watcher's startup and directory-arm sweeps announce in task_priority
order (urgent > normal > low, mtime FIFO within a tier), not glob/mtime
order -- closes #3017 and restores the ordering the single-decider redesign
moved out of the notifier's now-deleted pick.

Drives the real watch-tasks-stream.sh through a stubbed fswatch (same
pattern as the dedupe regression tests) so both the startup sweep and the
directory-level fallback arm are exercised without depending on a real
poll_monitor backend being selected on this platform.

Run: python3 tests/watch-tasks-stream-priority-sweep.test.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def wait_for(pred, timeout: float = 15.0, step: float = 0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


class Harness:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="priority-sweep-test-"))
        self.ws = self.tmp / "ws"
        (self.ws / "tasks").mkdir(parents=True)
        (self.ws / "results").mkdir()
        (self.ws / "state").mkdir()
        self.feed = self.tmp / "feed"
        self.feed.write_text("")
        stub_dir = self.tmp / "bin"
        stub_dir.mkdir()
        (stub_dir / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {self.feed}\n")
        (stub_dir / "fswatch").chmod(0o755)
        self.tasks_dir_abs = (self.ws / "tasks").resolve()
        self.proc: subprocess.Popen | None = None
        self.lines: list[str] = []

    def task(self, name: str, priority: str, age_s: float = 0.0) -> Path:
        p = self.ws / "tasks" / name
        p.write_text(f"id: {name[:-4]}\naccess_tier: owner\npriority: {priority}\ntask: probe\n")
        if age_s:
            now = time.time()
            os.utime(p, (now - age_s, now - age_s))
        return p

    def start(self) -> None:
        env = dict(os.environ)
        env["PATH"] = f"{self.tmp/'bin'}:{env['PATH']}"
        env["TMPDIR"] = str(self.tmp)
        env["SUTANDO_RESULTS_DIR"] = str(self.ws / "results")
        env.pop("SUTANDO_TASK_EVENT_HANDLER", None)
        env.pop("SUTANDO_INSTANCE_ID", None)
        self.proc = subprocess.Popen(
            ["bash", "src/watch-tasks-stream.sh", str(self.ws / "tasks")],
            cwd=str(REPO), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, start_new_session=True)

    def deliver_dir_event(self) -> None:
        with self.feed.open("a") as fh:
            fh.write(str(self.tasks_dir_abs) + "\n")

    def drain_stdout(self) -> None:
        try:
            os.set_blocking(self.proc.stdout.fileno(), False)
            c = self.proc.stdout.read()
            if c:
                self.lines.extend(c.splitlines())
        except Exception:
            pass

    def announce_order(self) -> list[str]:
        self.drain_stdout()
        return [l.split("TASK_FILE: ", 1)[1] for l in self.lines if l.startswith("TASK_FILE: ")]

    def cleanup(self) -> None:
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), 9)
            except Exception:
                pass
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
        try:
            import shutil
            shutil.rmtree(self.tmp, ignore_errors=True)
        except Exception:
            pass


def test_startup_sweep_announces_urgent_before_older_low():
    """task-low.txt is created FIRST (older mtime, would win a glob/mtime
    sweep); task-urgent.txt must still be announced first."""
    h = Harness()
    try:
        h.task("task-low.txt", "low", age_s=10)
        h.task("task-urgent.txt", "urgent")
        h.start()
        ok = wait_for(lambda: len(h.announce_order()) >= 2, timeout=15)
        order = h.announce_order()
        check("both tasks announced", ok, f"order so far: {order}")
        check("task-urgent.txt announced before task-low.txt",
              ok and order.index("task-urgent.txt") < order.index("task-low.txt"),
              f"order: {order}")
    finally:
        h.cleanup()


def test_directory_arm_sweep_also_orders_by_priority():
    """Same invariant through the directory-level fallback arm (poll_monitor's
    own granularity), not just the startup sweep."""
    h = Harness()
    try:
        h.start()
        wait_for(lambda: h.proc.poll() is None, timeout=5)
        h.task("task-low2.txt", "low", age_s=10)
        h.task("task-urgent2.txt", "urgent")
        h.deliver_dir_event()
        ok = wait_for(lambda: len(h.announce_order()) >= 2, timeout=15)
        order = h.announce_order()
        check("both tasks announced via the directory arm", ok, f"order so far: {order}")
        check("task-urgent2.txt announced before task-low2.txt",
              ok and order.index("task-urgent2.txt") < order.index("task-low2.txt"),
              f"order: {order}")
    finally:
        h.cleanup()


def main() -> int:
    test_startup_sweep_announces_urgent_before_older_low()
    test_directory_arm_sweep_also_orders_by_priority()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
