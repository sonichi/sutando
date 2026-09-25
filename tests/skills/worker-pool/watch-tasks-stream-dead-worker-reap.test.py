#!/usr/bin/env python3
"""A hung handler must not block the watcher forever: the watchdog
(SUTANDO_HANDLER_RUN_TIMEOUT) reaps it and the task still gets a genuine
terminal outcome, whether or not an answer already exists for it.

RETIRED (2026-09-20): every scenario here used to drive the async
#2603-era dispatch pipeline directly -- DISPATCH_DIR, its `workers/`
receipt files, killing a `--handler-runner` subprocess by pid, and the
production `kill_workers()` helper that reaped it. That pipeline (and the
concept of a "dead worker holding a dispatch slot") no longer exists:
run_handler_now() calls the handler synchronously, inline, and a handler
that never returns is bounded by its own watchdog, not a separate reap
sweep. The equivalent property -- a hung/dead handler does not strand a
task forever -- is what this file verifies now, against the real watcher.
"""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FAILURES: list[str] = []
FAILURE_TEXT = "could not safely process"


class ReadinessTimeout(RuntimeError):
    """A readiness wait expired. Never swallowed: a short kill leaves a worker alive."""


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def wait_for(pred, timeout: float = 30.0, step: float = 0.25, nudge=None) -> bool:
    """`nudge` re-arms the event that drives the drain. The reap runs on task
    ARRIVAL, so one delivery racing the loop's readiness can be consumed early."""
    end = time.time() + timeout
    ticks = 0
    while time.time() < end:
        if pred():
            return True
        ticks += 1
        if nudge is not None and ticks % 8 == 0:
            nudge(ticks)
        time.sleep(step)
    return False


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class Harness:
    """One isolated watcher: own workspace, TMPDIR and session (cleanup() ends in
    `kill 0`). Only pids recorded here are killed — never a pattern match.

    Imported by tests/watch-tasks-stream-interrupted-task-retry.test.py for
    this class and wait_for() -- keep their signatures stable."""

    def __init__(self, handler_timeout: str | None = None) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="reap-test-"))
        self.ws = self.tmp / "ws"
        (self.ws / "tasks").mkdir(parents=True)
        (self.ws / "results" / "archive").mkdir(parents=True)
        (self.ws / "state").mkdir()
        self.feed = self.tmp / "feed"
        self.feed.write_text("")
        # `tail -f` holds stdout open AND emits on demand: the stall is only
        # observable when a NEW task arrives, and arrival is what drains.
        stub_dir = self.tmp / "bin"
        stub_dir.mkdir()
        (stub_dir / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {self.feed}\n")
        (stub_dir / "fswatch").chmod(0o755)
        self.handler = self.tmp / "handler.sh"
        self.handler.write_text(
            '#!/bin/sh\nfor a in "$@"; do [ "$a" = "--probe" ] && exit 4; done\nexec sleep 100000\n')
        self.handler.chmod(0o755)
        self.handler_timeout = handler_timeout
        self.proc: subprocess.Popen | None = None

    @classmethod
    def attach(cls, ws: Path, tmp: Path) -> "Harness":
        """A second watcher over an existing workspace — the restart half of a
        durability check, which one process cannot demonstrate about itself."""
        h = cls.__new__(cls)
        h.tmp, h.ws, h.feed = tmp, ws, tmp / "feed2"
        h.feed.write_text("")
        (tmp / "bin" / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {h.feed}\n")
        (tmp / "bin" / "fswatch").chmod(0o755)
        h.handler, h.proc, h.handler_timeout = tmp / "handler.sh", None, None
        return h

    def task(self, name: str) -> Path:
        p = self.ws / "tasks" / name
        p.write_text(f"id: {name[:-4]}\naccess_tier: team\ntask: probe\n")
        return p

    def start(self) -> None:
        env = dict(os.environ)
        env["PATH"] = f"{self.tmp/'bin'}:{env['PATH']}"
        env["TMPDIR"] = str(self.tmp)
        env["SUTANDO_RESULTS_DIR"] = str(self.ws / "results")
        env["SUTANDO_TASK_EVENT_HANDLER"] = str(self.handler)
        if self.handler_timeout is not None:
            env["SUTANDO_HANDLER_RUN_TIMEOUT"] = self.handler_timeout
        # The watched dir is $1, NOT an env var — passing it as one would fall
        # through to the resolver and watch the REAL workspace.
        self.proc = subprocess.Popen(
            ["bash", "src/watch-tasks-stream.sh", str(self.ws / "tasks"), "--role", "standby", "--inbox", str(self.ws / "tasks")],
            cwd=str(REPO), env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)

    def deliver(self, name: str) -> None:
        p = self.task(name)
        with self.feed.open("a") as fh:
            fh.write(str(p.resolve()) + "\n")

    def stop(self, graceful: bool = False) -> None:
        """SIGKILL by default — fast and certain. `graceful` sends TERM to the
        watcher so its trap runs, which is the only way to observe teardown."""
        if self.proc is None:
            return
        if graceful:
            try:
                self.proc.send_signal(signal.SIGTERM)
                self.proc.wait(timeout=15)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def scenario_hung_handler_is_reaped_by_the_watchdog() -> None:
    """The stub handler probes must-handle then sleeps forever -- a genuinely
    dead/hung worker. The watchdog must still resolve the task."""
    print("\nscenario: a hung required-Team handler is reaped by the watchdog")
    h = Harness(handler_timeout="2")
    h.start()
    h.deliver("task-hung.txt")
    try:
        result = h.ws / "results" / "task-hung.txt"
        got = wait_for(result.is_file, 20.0)
        check("the watchdog-reaped handler still produces a terminal outcome", got)
        body = result.read_text() if result.is_file() else ""
        check("...and it's a safe refusal, not a silent drop or a fallback to core",
              FAILURE_TEXT in body, repr(body[:120]))
        check("...worded as a timeout, not conflated with an ordinary failure",
              "timed out" in body, repr(body[:120]))
    finally:
        h.stop()


def scenario_watchdog_reap_respects_an_existing_answer() -> None:
    """A genuine archived result for this task must still suppress the
    watchdog's terminal failure -- the fix is not "always publish"."""
    print("\nscenario: a watchdog reap does not overwrite an already-answered task")
    h = Harness(handler_timeout="2")
    (h.ws / "results" / "archive" / "task-answered-999.txt").write_text("the real answer\n")
    # `task-answered-1` must not satisfy `task-answered` (prefix collision) --
    # only the exact archived id above may suppress this task's failure.
    (h.ws / "results" / "archive" / "task-answered-1-999.txt").write_text("a different task\n")
    h.start()
    h.deliver("task-answered.txt")
    try:
        res = h.ws / "results" / "task-answered.txt"
        # No new live result should appear: the exact archived answer already
        # settles it. Give the watchdog its full window, then assert silence.
        time.sleep(4.0)
        check("an exact archived answer suppresses the watchdog's terminal failure",
              not res.exists() or FAILURE_TEXT not in res.read_text(),
              f"spurious failure written: {res.read_text()[:80] if res.exists() else ''}")
    finally:
        h.stop()


def main() -> int:
    for sc in (scenario_hung_handler_is_reaped_by_the_watchdog,
               scenario_watchdog_reap_respects_an_existing_answer):
        try:
            sc()
        except ReadinessTimeout as e:
            # Already recorded by check(); catching keeps one scenario from
            # hiding the verdicts of the others.
            print(f"  (scenario {sc.__name__} aborted: {e})")
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nPASS — a hung handler is reaped by the watchdog, and an existing answer is respected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
