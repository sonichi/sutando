#!/usr/bin/env python3
"""The directory-level fallback arm (poll_monitor reports the watched DIRECTORY,
not the file, on Created/Renamed/Updated -- see the comments beside
"$TASKS_DIR"|"$TASKS_DIR_ABS") in watch-tasks-stream.sh) must not re-dispatch a
task that is already in flight from an earlier dispatch (the startup sweep, or
the plain *.txt) per-file arm).

Before the fix: the directory arm tracked its own dedupe set
(WATCH_RUNTIME_DIR/dir-swept), written ONLY by that arm. The startup sweep and
the *.txt) arm called dispatch_task() directly with no marker at all, so a task
they had already dispatched -- and which is still sitting in tasks/ because it
has not finished/archived yet -- looked completely unswept to the directory arm
and was dispatched a SECOND time on the next directory-level event. Reviewed by
yixuan-ag2 on PR #4503, confirmed locally on poll_monitor (directory-only
events, zero file-level events on a rename-into-place).

After the fix: dispatch_task() itself owns one shared dedupe set
(WATCH_RUNTIME_DIR/dispatched), checked and written before any of the three
call sites can act, so all three share one dedupe set by construction.

This test drives the watcher directly through its event FIFO (a stubbed
fswatch that tails a feed file this test controls) rather than depending on a
real poll_monitor backend being selected on this platform -- it reproduces the
exact event SHAPE poll_monitor is documented to emit (a bare directory path,
no file path) and checks the dispatch count that shape produces.

Run: python3 tests/watch-tasks-stream-directory-fallback-dedup.test.py
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
        self.tmp = Path(tempfile.mkdtemp(prefix="dirfallback-test-"))
        self.ws = self.tmp / "ws"
        (self.ws / "tasks").mkdir(parents=True)
        (self.ws / "results" / "archive").mkdir(parents=True)
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

    def task(self, name: str) -> Path:
        p = self.ws / "tasks" / name
        p.write_text(f"id: {name[:-4]}\naccess_tier: team\ntask: probe\n")
        return p

    def start(self) -> None:
        env = dict(os.environ)
        env["PATH"] = f"{self.tmp/'bin'}:{env['PATH']}"
        env["TMPDIR"] = str(self.tmp)
        env["SUTANDO_RESULTS_DIR"] = str(self.ws / "results")
        # No handler declared -- dispatch_task emits "TASK_FILE: <name>" on
        # stdout directly. That's the dispatch-count signal this test reads.
        env.pop("SUTANDO_TASK_EVENT_HANDLER", None)
        env.pop("SUTANDO_INSTANCE_ID", None)
        self.proc = subprocess.Popen(
            ["bash", "src/watch-tasks-stream.sh", str(self.ws / "tasks")],
            cwd=str(REPO), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, start_new_session=True)

    def deliver_file_event(self, path: Path) -> None:
        with self.feed.open("a") as fh:
            fh.write(str(path.resolve()) + "\n")

    def deliver_dir_event(self) -> None:
        # The exact shape poll_monitor is documented to emit: the watched
        # DIRECTORY itself, never the file inside it.
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

    def dispatch_count(self, name: str) -> int:
        self.drain_stdout()
        return sum(1 for l in self.lines if l.strip() == f"TASK_FILE: {name}")

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


def test_startup_sweep_then_directory_event_dispatches_once():
    """The exact scenario from the review: a task already present at boot
    (so the startup sweep dispatches it) that is still in tasks/ (not yet
    archived/answered) when a later directory-level event re-observes it."""
    h = Harness()
    try:
        p = h.task("task-preexisting.txt")  # written BEFORE start -> startup sweep picks it up
        h.start()
        ok = wait_for(lambda: h.dispatch_count("task-preexisting.txt") >= 1, timeout=15)
        check("startup sweep dispatches the pre-existing task", ok,
              f"lines so far: {h.lines}")

        # Still sitting in tasks/, unarchived: the in-flight condition. A
        # directory-level event (poll_monitor's shape) re-observes it.
        h.deliver_dir_event()
        time.sleep(2.0)  # give a buggy build a fair chance to re-dispatch
        h.deliver_dir_event()
        time.sleep(2.0)

        count = h.dispatch_count("task-preexisting.txt")
        check("directory-level re-observation does not re-dispatch an in-flight task",
              count == 1, f"dispatched {count} times, expected 1; lines={h.lines}")
        check("task file was not garbled/removed by the dedupe path", p.exists())
    finally:
        h.cleanup()


def test_per_file_arm_then_directory_event_dispatches_once():
    """Same disjoint-dedupe shape, but through the *.txt) per-file arm instead
    of the startup sweep -- the review names both as unmarked callers."""
    h = Harness()
    try:
        h.start()
        ok = wait_for(lambda: h.proc.poll() is None, timeout=5)
        check("watcher process is up", ok)

        p = h.task("task-live.txt")
        h.deliver_file_event(p)
        ok = wait_for(lambda: h.dispatch_count("task-live.txt") >= 1, timeout=15)
        check("per-file arm dispatches the new task", ok, f"lines so far: {h.lines}")

        h.deliver_dir_event()
        time.sleep(2.0)

        count = h.dispatch_count("task-live.txt")
        check("directory-level re-observation after a per-file dispatch does not re-dispatch",
              count == 1, f"dispatched {count} times, expected 1; lines={h.lines}")
    finally:
        h.cleanup()


def test_two_distinct_tasks_each_dispatch_once():
    """The shared dedupe set must not over-suppress: two different files
    landing under the same directory-level event both get dispatched."""
    h = Harness()
    try:
        h.start()
        wait_for(lambda: h.proc.poll() is None, timeout=5)

        h.task("task-a.txt")
        h.task("task-b.txt")
        h.deliver_dir_event()

        ok_a = wait_for(lambda: h.dispatch_count("task-a.txt") >= 1, timeout=15)
        ok_b = wait_for(lambda: h.dispatch_count("task-b.txt") >= 1, timeout=15)
        check("task-a dispatched", ok_a, f"lines: {h.lines}")
        check("task-b dispatched", ok_b, f"lines: {h.lines}")

        h.deliver_dir_event()
        time.sleep(2.0)
        check("task-a still dispatched exactly once", h.dispatch_count("task-a.txt") == 1,
              f"lines: {h.lines}")
        check("task-b still dispatched exactly once", h.dispatch_count("task-b.txt") == 1,
              f"lines: {h.lines}")
    finally:
        h.cleanup()


def main() -> int:
    test_startup_sweep_then_directory_event_dispatches_once()
    test_per_file_arm_then_directory_event_dispatches_once()
    test_two_distinct_tasks_each_dispatch_once()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
