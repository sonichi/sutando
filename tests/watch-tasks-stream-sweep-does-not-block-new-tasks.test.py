#!/usr/bin/env python3
"""A restart sweep over a large inbox never holds back a task that arrives during it.

The sweep is planned by ONE helper process (task_dispatch.py sweep-plan, one
`--batch` resolver run) and fed into the same FIFO fswatch writes, so the event
loop reads a new task in arrival order while the sweep is still running, and a
file both paths see is admitted once.

Run: python3 tests/watch-tasks-stream-sweep-does-not-block-new-tasks.test.py
"""
from __future__ import annotations

import os
import sys
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from clean_watcher_env import clean_env  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
RESOLVER = REPO / "skills" / "worker-pool" / "scripts" / "resolve-inbox-entry"
W = "0123456789abcdef0123456789abcdef"
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def wait_for(pred, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


class Harness:
    """A worker inbox under a stubbed fswatch whose events this test writes."""

    def __init__(self, resolver: Path | None = None) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sweep-nonblock-")).resolve()
        self.ws = self.tmp / "ws"
        self.inbox = self.ws / "deliveries" / W
        for d in (self.ws / "tasks" / "archive", self.ws / "results" / "archive", self.ws / "state", self.inbox):
            d.mkdir(parents=True, exist_ok=True)
        # A workspace already migrated: these pointers stand for ones the archive step missed.
        (self.ws / "state" / "migrations").mkdir()
        (self.ws / "state" / "migrations" / "retire-archived-pointers.v1.done").write_text("{}\n")
        self.feed = self.tmp / "feed"
        self.feed.write_text("")
        (self.tmp / "bin").mkdir()
        stub = self.tmp / "bin" / "fswatch"
        stub.write_text(f"#!/bin/sh\nexec tail -n +1 -f {self.feed}\n")
        stub.chmod(0o755)
        self.resolver = resolver or RESOLVER
        self.out = self.tmp / "out"
        self.err = self.tmp / "err"
        self.proc: subprocess.Popen | None = None

    def stale(self, n: int) -> None:
        old = time.time() - 3600
        for i in range(n):
            tid = f"task-stale{i:05d}"
            (self.ws / "tasks" / "archive" / f"{tid}.txt").write_text(f"id: {tid}\ntask: done\n")
            (self.ws / "results" / "archive" / f"{tid}-1790000000.txt").write_text("answered\n")
            p = self.inbox / f"{tid}.txt"
            p.write_text("")
            os.utime(p, (old, old))

    def pending(self, tid: str, event: bool) -> Path:
        payload = self.ws / "tasks" / f"{tid}.txt"
        payload.write_text(f"id: {tid}\nsource: chat\ntask: new\n")
        ptr = self.inbox / f"{tid}.txt"
        ptr.write_text("")
        if event:
            with self.feed.open("a") as fh:
                fh.write(f"{ptr}\n")
        return payload

    def start(self) -> None:
        env = clean_env()
        env.update(PATH=f"{self.tmp / 'bin'}:{env['PATH']}", TMPDIR=str(self.tmp),
                   SUTANDO_INSTANCE_ID=W, SUTANDO_INSTANCE=W, SUTANDO_WORKSPACE_DIR=str(self.ws),
                   SUTANDO_RESULTS_DIR=str(self.ws / "results"), SUTANDO_INBOX_RESOLVER=str(self.resolver))
        env.pop("SUTANDO_TASK_EVENT_HANDLER", None)
        self.proc = subprocess.Popen(
            ["bash", "src/watch-tasks-stream.sh", str(self.inbox), "--role", "standby", "--inbox", str(self.inbox)],
            cwd=str(REPO), env=env, stdout=self.out.open("w"), stderr=self.err.open("w"),
            start_new_session=True)

    def emitted(self) -> list[str]:
        return [ln for ln in self.out.read_text().splitlines() if ln.startswith("TASK_FILE:")]

    def stderr(self) -> str:
        return self.err.read_text()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        shutil.rmtree(self.tmp, ignore_errors=True)


def large_stale_inbox() -> None:
    print("3,000 stale pointers and one new task")
    h = Harness()
    try:
        h.stale(3000)
        t0 = time.time()
        h.start()
        payload = h.pending("task-newone", event=True)
        got = wait_for(lambda: any(str(payload) in ln for ln in h.emitted()), 20)
        took = time.time() - t0
        print(f"       new task emitted {took:.2f}s after watcher start")
        check("the new task is emitted within a few seconds of watcher start", got and took < 8, f"{took:.2f}s")
        planned = wait_for(lambda: "sweep plan over 3001 entries: 3000 stale" in h.stderr(), 20)
        check("one plan covered every entry, 3000 of them stale", planned, h.stderr()[-400:])
        time.sleep(1)
        check("no stale pointer was emitted, and the new task once",
              h.emitted() == [f"TASK_FILE: {payload}"], repr(h.emitted()[:5]))
        check("no stale pointer cost a per-entry resolver run",
              "did not name an existing ABSOLUTE file" not in h.stderr()
              and "past the" not in h.stderr(), h.stderr()[-400:])
    finally:
        h.stop()


def new_task_read_while_sweep_runs() -> None:
    print("a slow sweep plan: a new task is read before it finishes; a file both paths see is admitted once")
    h = Harness()
    slow = h.tmp / "slow-resolver"
    slow.write_text(f"""#!/bin/sh
if [ "$1" = "--batch" ]; then
  sleep 4
  echo "resolve-inbox-entry batch v1"
  while IFS= read -r e; do
    p="{h.ws}/tasks/$(basename "$e")"
    if [ -f "$p" ]; then printf '0\\t%s\\t%s\\n' "$p" "$e"; else printf '3\\t\\t%s\\n' "$e"; fi
  done
  exit 0
fi
p="{h.ws}/tasks/$(basename "$1")"
[ -f "$p" ] && {{ echo "$p"; exit 0; }}
exit 3
""")
    slow.chmod(0o755)
    h.resolver = slow
    try:
        h.stale(20)
        both = h.pending("task-seenbyboth", event=False)
        h.start()
        with h.feed.open("a") as fh:
            fh.write(f"{h.inbox / 'task-seenbyboth.txt'}\n")
        time.sleep(0.5)
        fresh = h.pending("task-midsweep", event=True)
        got = wait_for(lambda: any(str(fresh) in ln for ln in h.emitted()), 3)
        plan_done = "sweep plan over" in h.stderr()
        check("a task arriving mid-sweep is emitted while the plan is still running", got and not plan_done,
              f"emitted={got} plan_done={plan_done}")
        wait_for(lambda: "sweep plan over" in h.stderr(), 15)
        time.sleep(1.5)
        lines = h.emitted()
        check("the file seen by fswatch and by the sweep is emitted exactly once",
              lines.count(f"TASK_FILE: {both}") == 1, repr(lines))
        check("nothing else was emitted", sorted(lines) == sorted([f"TASK_FILE: {both}", f"TASK_FILE: {fresh}"]),
              repr(lines))
    finally:
        h.stop()


if __name__ == "__main__":
    large_stale_inbox()
    new_task_read_while_sweep_runs()
    print()
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s) failed")
        raise SystemExit(1)
    print("PASS")
