#!/usr/bin/env python3
"""A bare-directory event from fswatch dispatches nothing; the file event
dispatches exactly once; the startup sweep dispatches a pre-existing task once.

fswatch's default macOS backend emits the watched directory itself as the
first event after a change, then the file path. The watcher used to answer
the bare-directory event with a sweep of every *.txt still in tasks/, which
re-announced a task already in flight. The sweep is gone; the per-file event
and the startup sweep are the only dispatch paths, so no task is announced
twice within one watcher lifetime.

The watcher is driven through its event FIFO by a stubbed fswatch that tails
a feed file this test controls, so the exact event shapes are reproduced
without depending on the platform backend.

Run: python3 tests/watch-tasks-stream-bare-directory-event.test.py
"""
from __future__ import annotations

import os
import shutil
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
        self.tmp = Path(tempfile.mkdtemp(prefix="baredir-test-"))
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
        for k in ("SUTANDO_TASK_EVENT_HANDLER", "SUTANDO_INSTANCE_ID", "SUTANDO_TASKS_DIR",
                  "SUTANDO_WORKSPACE_DIR", "SUTANDO_INBOX_KIND"):
            env.pop(k, None)
        self.proc = subprocess.Popen(
            ["bash", "src/watch-tasks-stream.sh", str(self.ws / "tasks")],
            cwd=str(REPO), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, start_new_session=True)

    def deliver_file_event(self, path: Path) -> None:
        with self.feed.open("a") as fh:
            fh.write(str(path.resolve()) + "\n")

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
        shutil.rmtree(self.tmp, ignore_errors=True)


def test_bare_directory_event_dispatches_nothing_after_startup_sweep():
    h = Harness()
    try:
        h.task("task-preexisting.txt")
        h.start()
        check("startup sweep dispatches the pre-existing task once",
              wait_for(lambda: h.dispatch_count("task-preexisting.txt") == 1))
        h.deliver_dir_event()
        time.sleep(2.0)
        check("a bare-directory event re-dispatches nothing",
              h.dispatch_count("task-preexisting.txt") == 1,
              f"count={h.dispatch_count('task-preexisting.txt')}")
    finally:
        h.cleanup()


def test_file_event_dispatches_once_and_bare_directory_event_never_repeats_it():
    h = Harness()
    try:
        h.start()
        time.sleep(1.0)
        h.deliver_dir_event()
        p = h.task("task-new.txt")
        h.deliver_dir_event()
        h.deliver_file_event(p)
        check("the file event dispatches the new task once",
              wait_for(lambda: h.dispatch_count("task-new.txt") == 1))
        h.deliver_dir_event()
        time.sleep(2.0)
        check("later bare-directory events never repeat it",
              h.dispatch_count("task-new.txt") == 1,
              f"count={h.dispatch_count('task-new.txt')}")
    finally:
        h.cleanup()


def test_eof_from_fswatch_ends_the_watcher():
    h = Harness()
    try:
        h.start()
        time.sleep(1.0)
        # The stub tails the feed; killing it closes the FIFO's write end.
        subprocess.run(["pkill", "-f", f"tail -n \\+1 -f {h.feed}"], check=False)
        check("EOF on the event stream ends the watcher",
              wait_for(lambda: h.proc.poll() is not None, timeout=10.0))
    finally:
        h.cleanup()


if __name__ == "__main__":
    for fn in (
        test_bare_directory_event_dispatches_nothing_after_startup_sweep,
        test_file_event_dispatches_once_and_bare_directory_event_never_repeats_it,
        test_eof_from_fswatch_ends_the_watcher,
    ):
        print(fn.__name__)
        fn()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        raise SystemExit(1)
    print("OK")
