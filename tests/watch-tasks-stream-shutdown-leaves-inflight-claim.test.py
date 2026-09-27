#!/usr/bin/env python3
"""A watcher killed while its task handler is in flight decides nothing.

Before, `settle_own_claims_on_shutdown()` read an unfinished claim as "never
handled" and announced the task to the live core -- but the handler (the pool
router) had usually already delivered it, so the same task ran twice (#4816).
Now the shutdown leaves the claim behind, still naming the dead watcher's pid;
the next watcher retires it in `prepare_handler_state()`, re-sweeps `tasks/` and
runs the handler again, and the handler's delivery is idempotent (one sentinel,
created with O_EXCL under a lock; a second pass reports "already").

The stub handler here models exactly that contract: a sentinel created with
`set -C`, one log line per outcome. Each arm is one kill point from #4825 and
ends with EXACTLY ONE delivery and a released claim:

  a. before the probe      -- nothing claimed; the next sweep delivers once
  b. during routing        -- (b1) before the sentinel: the next pass delivers
                              (b2) after the sentinel: the next pass says already
  c/d. after delivery, before the done record / before the release -- the state
       a kill there leaves (sentinel present, claim naming a dead pid) is what
       the next watcher sees; it must say already and release, never re-deliver

On the parent commit arms b1/b2 fail: the first watcher's shutdown announces
the task (`TASK_FILE:` on stdout), the duplicate this pins out.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "fixtures"))
from clean_watcher_env import clean_env  # noqa: E402

FAILURES: list[str] = []
LEAVING = "to the next watcher's sweep"

HANDLER = r'''#!/bin/sh
# probe: accept. run: idempotent delivery (sentinel via noclobber), then the mode.
for a in "$@"; do [ "$a" = "--probe" ] && exit 0; done
task=""; prev=""
for a in "$@"; do [ "$prev" = "--task-file" ] && task="$a"; prev="$a"; done
id=$(basename "$task" .txt)
mode=$(cat "$CTRL/mode" 2>/dev/null)
echo "run $id $mode" >> "$CTRL/log"
[ "$mode" = "hang-before" ] && exec sleep 100000
if ( set -C; : > "$DELIV/$id.pending" ) 2>/dev/null; then
  echo "delivered $id" >> "$CTRL/log"
else
  echo "already $id" >> "$CTRL/log"
fi
[ "$mode" = "hang-after" ] && exec sleep 100000
exit 0
'''


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def wait_for(pred, timeout: float, step: float = 0.1) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


class Workspace:
    """One scratch workspace; several watchers may run over it in turn."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="leave-claim-"))
        self.ws = self.tmp / "ws"
        (self.ws / "tasks").mkdir(parents=True)
        (self.ws / "results" / "archive").mkdir(parents=True)
        (self.ws / "state").mkdir()
        self.ctrl = self.tmp / "ctrl"
        self.ctrl.mkdir()
        self.deliv = self.tmp / "deliv"
        self.deliv.mkdir()
        self.handler = self.tmp / "handler.sh"
        self.handler.write_text(HANDLER)
        self.handler.chmod(0o755)
        (self.tmp / "bin").mkdir()
        self.n = 0

    @property
    def claims(self) -> Path:
        return self.ws / "state" / "task-event-handler-claims"

    def mode(self, m: str) -> None:
        (self.ctrl / "mode").write_text(m)

    def log(self) -> list[str]:
        p = self.ctrl / "log"
        return p.read_text().splitlines() if p.is_file() else []

    def task(self, name: str) -> Path:
        p = self.ws / "tasks" / name
        p.write_text(f"id: {name[:-4]}\naccess_tier: owner\ntask: probe\n")
        return p

    def sentinels(self) -> list[str]:
        return sorted(p.name for p in self.deliv.glob("*.pending"))

    def claim_pid(self, name: str) -> str:
        c = self.claims / name
        return c.read_text().splitlines()[0] if c.is_file() else ""


class Watcher:
    """One watcher over the workspace, stdout and stderr captured to files."""

    def __init__(self, w: Workspace) -> None:
        w.n += 1
        self.w, self.n = w, w.n
        self.feed = w.tmp / f"feed{self.n}"
        self.feed.write_text("")
        # A stub fswatch: `tail -f` holds stdout open and emits on demand.
        (w.tmp / "bin" / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {self.feed}\n")
        (w.tmp / "bin" / "fswatch").chmod(0o755)
        self.out = w.tmp / f"w{self.n}.out"
        self.err = w.tmp / f"w{self.n}.err"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = clean_env()
        env["PATH"] = f"{self.w.tmp/'bin'}:{env['PATH']}"
        env["TMPDIR"] = str(self.w.tmp)
        env["SUTANDO_RESULTS_DIR"] = str(self.w.ws / "results")
        env["SUTANDO_TASK_EVENT_HANDLER"] = str(self.w.handler)
        env["CTRL"] = str(self.w.ctrl)
        env["DELIV"] = str(self.w.deliv)
        inbox = str(self.w.ws / "tasks")
        self.proc = subprocess.Popen(
            ["bash", "src/watch-tasks-stream.sh", inbox, "--role", "standby", "--inbox", inbox],
            cwd=str(REPO), env=env, stdout=self.out.open("w"), stderr=self.err.open("w"),
            start_new_session=True)

    def deliver(self, name: str) -> Path:
        p = self.w.task(name)
        with self.feed.open("a") as fh:
            fh.write(str(p.resolve()) + "\n")
        return p

    def announced(self, name: str) -> int:
        return sum(1 for ln in self.out.read_text().splitlines()
                   if ln.startswith("TASK_FILE:") and name in ln)

    def stop(self, graceful: bool = False) -> None:
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


def handler_running(w: Workspace, name: str) -> bool:
    return any(ln.startswith(f"run {name[:-4]} ") for ln in w.log())


def expect_settled(w: Workspace, w2: Watcher, name: str, want_delivered: int, want_already: int) -> None:
    tid = name[:-4]
    def outcomes() -> int:
        return sum(1 for ln in w.log() if ln in (f"delivered {tid}", f"already {tid}"))

    # "claim absent" is also true BEFORE the sweep claims it: wait for the
    # handler's outcome first, then for the release that follows it.
    check("the next watcher's sweep ran the handler to an outcome",
          wait_for(lambda: outcomes() >= want_delivered + want_already, 30.0), str(w.log()))
    check("the next watcher releases the claim after its sweep",
          wait_for(lambda: not (w.claims / name).exists(), 30.0), w.claim_pid(name))
    log = w.log()
    check("exactly one delivery ever happened",
          sum(1 for ln in log if ln == f"delivered {tid}") == want_delivered and w.sentinels() == [f"{tid}.pending"],
          f"log={log} sentinels={w.sentinels()}")
    check("the second pass reported 'already' as many times as expected",
          sum(1 for ln in log if ln == f"already {tid}") == want_already, str(log))
    check("the next watcher announced nothing to the live core either", w2.announced(name) == 0)
    check("no result was published for the task",
          not list((w.ws / "results").glob(f"*{tid}*")))


def arm_before_probe() -> None:
    print("\narm a: killed before the probe — the task is just a file in tasks/")
    w = Workspace()
    w.mode("normal")
    w.task("task-a.txt")           # nothing ever claimed it
    w2 = Watcher(w)
    try:
        w2.start()
        expect_settled(w, w2, "task-a.txt", want_delivered=1, want_already=0)
    finally:
        w2.stop()


def arm_during_routing(sub: str, mode: str, want_delivered: int, want_already: int) -> None:
    print(f"\narm {sub}: TERM while the handler is in flight ({mode})")
    w = Workspace()
    w.mode(mode)
    w1 = Watcher(w)
    w2 = None
    try:
        w1.start()
        w1.deliver("task-b.txt")
        check("the handler is running when the TERM lands",
              wait_for(lambda: handler_running(w, "task-b.txt"), 30.0))
        time.sleep(0.5)          # let a hang-after handler write its sentinel
        w1.stop(graceful=True)   # SIGTERM -> settle_own_claims_on_shutdown()
        time.sleep(1.0)
        check("the dying watcher announced NO TASK_FILE for it", w1.announced("task-b.txt") == 0,
              w1.out.read_text()[-200:])
        check("...and logged that it left the task to the next watcher",
              LEAVING in w1.err.read_text(), w1.err.read_text()[-300:])
        check("the claim is left behind naming the dead watcher's pid",
              w.claim_pid("task-b.txt") == str(w1.proc.pid), w.claim_pid("task-b.txt"))
        check("no result was published at shutdown",
              not list((w.ws / "results").glob("*task-b*")))
        w.mode("normal")
        w2 = Watcher(w)
        w2.start()
        expect_settled(w, w2, "task-b.txt", want_delivered, want_already)
    finally:
        w1.stop()
        if w2 is not None:
            w2.stop()


def dead_pid() -> str:
    p = subprocess.Popen(["sh", "-c", "true"])
    p.wait()
    return str(p.pid)


def arm_after_delivery(sub: str, label: str) -> None:
    print(f"\narm {sub}: killed {label} — sentinel present, claim naming a dead pid")
    w = Workspace()
    w.mode("normal")
    task = w.task("task-c.txt")
    (w.deliv / "task-c.pending").touch()              # the delivery already happened
    w.claims.mkdir(parents=True)
    (w.claims / "task-c.txt").write_text(f"{dead_pid()}\nsome-dead-watcher\n{task}\nfallback\n")
    w2 = Watcher(w)
    try:
        w2.start()
        expect_settled(w, w2, "task-c.txt", want_delivered=0, want_already=1)
    finally:
        w2.stop()


def main() -> int:
    arm_before_probe()
    arm_during_routing("b1", "hang-before", want_delivered=1, want_already=0)
    arm_during_routing("b2", "hang-after", want_delivered=1, want_already=1)
    arm_after_delivery("c", "after delivery, before the done record")
    arm_after_delivery("d", "after the done record, before the release")
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nPASS — a kill at any phase ends with exactly one delivery, no announce, "
          "no refusal, and a claim the next watcher retires and releases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
