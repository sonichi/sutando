#!/usr/bin/env python3
"""The watcher's startup sweep announces in task_priority order (urgent >
normal > low, mtime FIFO within a tier), not glob/mtime order -- closes
#3017 and restores the ordering the single-decider redesign moved out of
the notifier's now-deleted pick.

Drives the real watch-tasks-stream.sh through a stubbed fswatch (same
pattern as the dedupe regression tests). #4560 deleted the directory-level
fallback arm entirely (a bare-folder event now dispatches nothing, per
watch-tasks-stream-bare-directory-event.test.py), so there is no second
sweep site left to exercise here.

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
            ["bash", "src/watch-tasks-stream.sh", str(self.ws / "tasks"), "--role", "standby", "--inbox", str(self.ws / "tasks")],
            cwd=str(REPO), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, start_new_session=True)

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


def test_helper_failure_falls_back_to_mtime_order_instead_of_dropping_the_backlog():
    """If the priority-sort helper exits 0 with no output (the exact shape a
    stubbed SUTANDO_PY producing nothing takes), the sweep must still dispatch
    every task -- in mtime order -- rather than silently announcing none."""
    h = Harness()
    try:
        stub_py = h.tmp / "bin" / "stub-python3"
        stub_py.write_text("#!/bin/sh\nexit 0\n")
        stub_py.chmod(0o755)
        h.task("task-a3.txt", "urgent", age_s=10)
        h.task("task-b3.txt", "low")

        env = dict(os.environ)
        env["PATH"] = f"{h.tmp/'bin'}:{env['PATH']}"
        env["TMPDIR"] = str(h.tmp)
        env["SUTANDO_RESULTS_DIR"] = str(h.ws / "results")
        env["SUTANDO_PY"] = str(stub_py)
        env.pop("SUTANDO_TASK_EVENT_HANDLER", None)
        env.pop("SUTANDO_INSTANCE_ID", None)
        h.proc = subprocess.Popen(
            ["bash", "src/watch-tasks-stream.sh", str(h.ws / "tasks"), "--role", "standby", "--inbox", str(h.ws / "tasks")],
            cwd=str(REPO), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, start_new_session=True)

        ok = wait_for(lambda: len(h.announce_order()) >= 2, timeout=15)
        order = h.announce_order()
        check("both tasks still announced despite the empty helper output", ok, f"order so far: {order}")
        check("dispatched in mtime order (fallback, not priority)",
              ok and set(order[:2]) == {"task-a3.txt", "task-b3.txt"},
              f"order: {order}")
    finally:
        h.cleanup()


def main() -> int:
    test_startup_sweep_announces_urgent_before_older_low()
    test_helper_failure_falls_back_to_mtime_order_instead_of_dropping_the_backlog()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
