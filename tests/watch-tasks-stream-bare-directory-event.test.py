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
import sys  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.process_group import popen_bound  # noqa: E402

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

    def start(self, **extra_env: str) -> None:
        env = dict(os.environ)
        env["PATH"] = f"{self.tmp/'bin'}:{env['PATH']}"
        env.update(extra_env)
        env["TMPDIR"] = str(self.tmp)
        env["SUTANDO_RESULTS_DIR"] = str(self.ws / "results")
        for k in ("SUTANDO_TASK_EVENT_HANDLER", "SUTANDO_INSTANCE_ID", "SUTANDO_TASKS_DIR",
                  "SUTANDO_WORKSPACE_DIR", "SUTANDO_INBOX_KIND"):
            env.pop(k, None)
        self.proc = popen_bound(
            ["bash", "src/watch-tasks-stream.sh", str(self.ws / "tasks"), "--role", "standby", "--inbox", str(self.ws / "tasks")],
            cwd=str(REPO), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)

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


def test_eof_from_fswatch_relaunches_it_and_ends_the_watcher_only_past_the_budget():
    h = Harness()
    tail = ["pgrep", "-f", f"tail -n \\+1 -f {h.feed}"]
    tails = lambda: set(subprocess.run(tail, capture_output=True, text=True).stdout.split())

    def startup_accepted():
        try:
            return h.proc.poll() is None and any(
                p.read_text().strip() == str(h.proc.pid)
                for p in (h.ws / "state").glob("watch-tasks-stream*.pid"))
        except OSError:
            return False

    try:
        h.start(SUTANDO_FSWATCH_RESTART_MAX="1")
        ready = wait_for(startup_accepted)
        check("fswatch startup is accepted before its writer is stopped", ready,
              f"rc={h.proc.poll()}")
        if not ready:
            return
        first = tails()
        check("the initial fswatch writer is running", bool(first))
        if not first:
            return
        # The stub tails the feed; killing it closes the FIFO's write end.
        subprocess.run(["pkill", "-f", f"tail -n \\+1 -f {h.feed}"], check=False)
        relaunched = wait_for(lambda: tails() - first and h.proc.poll() is None, timeout=10.0)
        check("EOF on the event stream relaunches fswatch, the watcher stays", relaunched)
        if not relaunched:
            return
        task = h.task("task-after-relaunch.txt")
        h.deliver_file_event(task)
        serving = wait_for(lambda: h.dispatch_count(task.name) == 1 and h.proc.poll() is None)
        check("the relaunched watcher serves a task before its writer is stopped", serving)
        if not serving:
            return
        subprocess.run(["pkill", "-f", f"tail -n \\+1 -f {h.feed}"], check=False)
        check("a second EOF past the relaunch budget ends the watcher, status 1",
              wait_for(lambda: h.proc.poll() == 1, timeout=10.0), f"rc={h.proc.poll()}")
    finally:
        h.cleanup()


if __name__ == "__main__":
    for fn in (
        test_bare_directory_event_dispatches_nothing_after_startup_sweep,
        test_file_event_dispatches_once_and_bare_directory_event_never_repeats_it,
        test_eof_from_fswatch_relaunches_it_and_ends_the_watcher_only_past_the_budget,
    ):
        print(fn.__name__)
        fn()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        raise SystemExit(1)
    print("OK")
