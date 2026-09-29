#!/usr/bin/env python3
"""A watcher killed while its task handler is in flight decides nothing.

Before, `settle_own_claims_on_shutdown()` read an unfinished claim as "never
handled" and announced the task to the live core -- but the handler (the pool
router) had usually already delivered it, so the same task ran twice (#4816).
Now the shutdown leaves the claim behind, still naming the dead watcher's pid
and start time; the next watcher retires it in `reconcile_dead_claims()` before
its first dispatch, re-sweeps `tasks/` and runs the router again; and the
router's replay follows the delivery it already committed to (one sentinel, one
attribution) rather than the current binding.

This drives the REAL watcher with the REAL `pool_route_handler.py` on a fixture
workspace (roster with two live workers, one bound room). The kill points are
the ones in #4825, each hit by a real kill: three through the watcher's
documented transition hook (`SUTANDO_WATCHER_TRANSITION_HOOK`, a dependency
seam that runs synchronously inside the transition), two through a handler
wrapper that lingers before or after the router runs.

  a. before the claim         -- barrier `before-claim`
  b1/b2. during routing       -- the wrapper lingers before / after the router
  c. after delivery, before the done record -- barrier `after-handler`
  d. after the done record, before the release -- barrier `after-done`
     (on the core path the done writer is a no-op without an instance id, so
     c and d are two TERM locations over the SAME durable state: claim by this
     pid, sentinel in A, attribution A; no done-record transition is claimed)
  e1/e2. the left-behind claim's pid recycled / its true owner still alive
  e3. the PRODUCTION writer's claim, taken from a live paused watcher, carries
      the start time in the C locale; a reader started under another locale
      still calls it live (reverting the writer to four fields fails here)
  e4. the dead watcher is left a zombie (unreaped): its production claim is retired
  e5. a reader whose `ps` fails keeps a live owner's claim and says so, then
      retires the dead owner on its next dispatch once `ps` answers again
  e6. a legacy four-line claim (the writer before this change) is read by pid alone
  f. the next watcher has NO handler declared: the handler's dead claim is held
     (both production notifiers' own claim guard, `filename_is_claimed`, would
     drop a line for it), never announced; when a handler returns it replays once
  g. the room is rebound A->B between the kill and the replay: the replay
     finishes the delivery to A and never delivers to B
  h. a no-handler watcher meets a LIVE claim for a task ALREADY delivered to A
     (unknown owner while its ps fails): zero TASK_FILE lines, no admission; when
     the owner dies the claim is preserved and the task held (a handler's outcome
     is unknown and no handler is here); when a handler returns it replays as
     "already": one sentinel, attribution A, never announced
  h2. the same with the owner dead BEFORE delivering: held, never announced; the
      returning handler routes it exactly once
  l. a handler-enabled session watcher sweeps while the standby owner is alive
     (claim lost, rc 1): held; the owner dies (TERM, and KILL); routed exactly
     once on the timer, zero core lines
  i. a dead handler claim met by the task's own dispatch in a no-handler watcher:
     held, never announced; the returning handler replays it exactly once
  j. a writer whose ps fails refuses to claim a task that nobody holds, holds it,
     and routes it exactly once on its own timer after ps returns
  k. fswatch closes while a task is held: genuine EOF still ends the watcher

Every arm ends with exactly one sentinel across every recipient folder, the
attribution naming that recipient, and no claim left behind; a bound task is
never announced to the live core by either watcher.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "fixtures"))
sys.path.insert(0, str(REPO / "skills" / "worker-pool" / "scripts"))
sys.path.insert(0, str(REPO / "src"))
from clean_watcher_env import clean_env  # noqa: E402

import pool_attribution as pa  # noqa: E402

A = "a" * 32
B = "b" * 32
ROOM = "!bound:example.test"
ROUTER = REPO / "skills" / "worker-pool" / "scripts" / "pool_route_handler.py"
FAILURES: list[str] = []
LEAVING = "to the next watcher's sweep"

# The real router, with a linger before or after it so a kill lands mid-route.
# The transition hook: sleeps inside the named transition so a TERM lands there.
HOOK = '''#!/bin/bash
[ "$1" = "$(cat "$CTRL/pause-at" 2>/dev/null)" ] || exit 0
echo "hook $1 $2" >> "$CTRL/log"
sleep 60
'''

HANDLER = '''#!/bin/bash
mode=$(cat "$CTRL/mode" 2>/dev/null)
case " $* " in *" --probe "*) exec python3 "$ROUTER" "$@" ;; esac
echo "run $mode" >> "$CTRL/log"
[ "$mode" = "hang-before" ] && exec sleep 100000
python3 "$ROUTER" "$@"; rc=$?
echo "router rc=$rc" >> "$CTRL/log"
[ "$mode" = "hang-after" ] && exec sleep 100000
exit $rc
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


_TREES: list[Path] = []
atexit.register(lambda: [shutil.rmtree(t, ignore_errors=True) for t in _TREES])


class Workspace:
    """One fixture workspace; several watchers may run over it in turn."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="leave-claim-"))
        _TREES.append(self.tmp)
        self.ws = self.tmp / "ws"
        for d in ("tasks", "results/archive", "state", "deliveries"):
            (self.ws / d).mkdir(parents=True)
        self.bind(A)
        self.ctrl = self.tmp / "ctrl"
        self.ctrl.mkdir()
        self.handler = self.tmp / "handler.sh"
        self.handler.write_text(HANDLER)
        self.handler.chmod(0o755)
        self.hook = self.tmp / "hook.sh"
        self.hook.write_text(HOOK)
        self.hook.chmod(0o755)
        (self.tmp / "bin").mkdir()
        self.n = 0
        self.mode("normal")

    def bind(self, worker: str) -> None:
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {A: {"state": "live"}, B: {"state": "live"}},
             "bindings": {ROOM: worker}}))

    @property
    def claims(self) -> Path:
        return self.ws / "state" / "task-event-handler-claims"

    def mode(self, m: str) -> None:
        (self.ctrl / "mode").write_text(m)

    def pause_at(self, transition: str) -> None:
        (self.ctrl / "pause-at").write_text(transition)

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def log(self) -> list[str]:
        p = self.ctrl / "log"
        return p.read_text().splitlines() if p.is_file() else []

    def task(self, name: str) -> Path:
        p = self.ws / "tasks" / name
        p.write_text(f"id: {name[:-4]}\nchannel_id: {ROOM}\nsource: ag2space\naccess_tier: owner\ntask: body\n")
        return p

    def sentinels(self, tid: str) -> list[str]:
        return sorted(str(p.relative_to(self.ws)) for p in (self.ws / "deliveries").glob(f"*/{tid}.*"))

    def attribution(self, tid: str) -> str | None:
        return pa.worker_for_task(self.ws, tid)

    def claim_pid(self, name: str) -> str:
        c = self.claims / name
        return c.read_text().splitlines()[0] if c.is_file() else ""


class Watcher:
    """One watcher over the workspace, stdout and stderr captured to files."""

    def __init__(self, w: Workspace, handler: bool = True, role: str = "standby") -> None:
        w.n += 1
        self.w, self.n, self.handler, self.role = w, w.n, handler, role
        self.feed = w.tmp / f"feed{self.n}"
        self.feed.write_text("")
        # A stub fswatch: `tail -f` holds stdout open and emits on demand.
        (w.tmp / "bin" / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {self.feed}\n")
        (w.tmp / "bin" / "fswatch").chmod(0o755)
        self.out = w.tmp / f"w{self.n}.out"
        self.err = w.tmp / f"w{self.n}.err"
        self.proc: subprocess.Popen | None = None

    def start(self, extra_env: dict | None = None) -> None:
        env = clean_env()
        env.update(extra_env or {})
        env["PATH"] = f"{self.w.tmp/'bin'}:{env['PATH']}"
        env["TMPDIR"] = str(self.w.tmp)
        env["SUTANDO_RESULTS_DIR"] = str(self.w.ws / "results")
        if self.handler:
            env["SUTANDO_TASK_EVENT_HANDLER"] = str(self.w.handler)
        env["SUTANDO_WATCHER_TRANSITION_HOOK"] = str(self.w.hook)
        env["CTRL"] = str(self.w.ctrl)
        env["ROUTER"] = str(ROUTER)
        inbox = str(self.w.ws / "tasks")
        # A second standby on one inbox is refused by design; a SESSION watcher
        # takes over from a standby that has not stood down after this timeout.
        env["SUTANDO_STANDBY_STOP_TIMEOUT"] = "2"
        env["SUTANDO_HELD_RETRY_INTERVAL"] = "2"
        env["SUTANDO_WATCHER_START_LOCK_TIMEOUT_S"] = "3"   # the lock is held for a watcher's lifetime
        env["SUTANDO_FSWATCH_RESTART_MAX"] = "0"   # arm k drives the exit through a killed fswatch's EOF
        self.proc = subprocess.Popen(
            ["bash", "src/watch-tasks-stream.sh", inbox, "--role", self.role, "--inbox", inbox],
            cwd=str(REPO), env=env, stdout=self.out.open("w"), stderr=self.err.open("w"),
            start_new_session=True)
        if self.role == "session":
            # A session watcher proves readiness by seeing its own probe file come
            # back through fswatch; the stub replays the feed, so echo the probes.
            threading.Thread(target=self._echo_probes, daemon=True).start()

    def _echo_probes(self) -> None:
        seen: set[str] = set()
        while self.proc is not None and self.proc.poll() is None:
            for probe in (self.w.ws / "tasks").glob(".ready-*"):
                if str(probe) not in seen:
                    seen.add(str(probe))
                    with self.feed.open("a") as fh:
                        fh.write(str(probe.resolve()) + "\n")
            time.sleep(0.1)

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


def claimed(w: Workspace, name: str) -> bool:
    return (w.claims / name).is_file()


def expect_left_behind(w: Workspace, w1: Watcher, name: str, delivered_before_kill: bool) -> None:
    tid = name[:-4]
    time.sleep(1.0)
    check("the dying watcher announced NO TASK_FILE for it", w1.announced(name) == 0, w1.out.read_text()[-200:])
    check("...and logged that it left the task to the next watcher", LEAVING in w1.err.read_text(),
          w1.err.read_text()[-300:])
    check("the claim is left behind naming the dead watcher's pid", w.claim_pid(name) == str(w1.proc.pid),
          w.claim_pid(name))
    check("no result was published at shutdown", not list((w.ws / "results").glob(f"*{tid}*")))
    want = [f"deliveries/{A}/{tid}.txt"] if delivered_before_kill else []
    check("the delivery state at the kill is as the arm names it", w.sentinels(tid) == want, str(w.sentinels(tid)))


def router_runs(w: Workspace) -> int:
    return sum(1 for ln in w.log() if ln.startswith("router rc="))


def expect_settled(w: Workspace, w2: Watcher, name: str, runs: int, recipient: str = A) -> None:
    tid = name[:-4]
    # "claim absent" and "sentinel present" can both be true BEFORE the replay
    # runs: wait for the router's Nth completion, then for the release after it.
    check("the next watcher's sweep replayed the router to completion",
          wait_for(lambda: router_runs(w) >= runs, 30.0), str(w.log()))
    check("the next watcher releases the claim after its sweep",
          wait_for(lambda: not claimed(w, name), 30.0), w.claim_pid(name))
    check("...and the sentinel is in place",
          w.sentinels(tid) == [f"deliveries/{recipient}/{tid}.txt"], str(w.sentinels(tid)))
    check("exactly one sentinel exists across every recipient folder",
          w.sentinels(tid) == [f"deliveries/{recipient}/{tid}.txt"], str(w.sentinels(tid)))
    check("the attribution names that recipient", w.attribution(tid) == recipient, str(w.attribution(tid)))
    check("the next watcher announced nothing to the live core either", w2.announced(name) == 0)
    check("no result was published for the task", not list((w.ws / "results").glob(f"*{tid}*")))


def poke(w: Workspace, w2: Watcher, name: str) -> None:
    """Deliver an unrelated task and wait for the router to finish it: proof that
    the watcher dispatched (and re-asked every claim) after this point."""
    before = router_runs(w)
    w2.deliver(name)
    check(f"the poke {name} was dispatched and routed", wait_for(lambda: router_runs(w) > before, 30.0), str(w.log()))


def in_hook(w: Workspace, transition: str) -> bool:
    return any(ln.startswith(f"hook {transition} ") for ln in w.log())


def arm_barrier(sub: str, transition: str, delivered_before_kill: bool) -> None:
    print(f"\narm {sub}: TERM inside the watcher's `{transition}` transition")
    w = Workspace()
    w.pause_at(transition)
    w1 = Watcher(w)
    w2 = None
    try:
        w1.start()
        w1.deliver("task-p.txt")
        check("the watcher is inside the named transition when the TERM lands",
              wait_for(lambda: in_hook(w, transition), 30.0), str(w.log()))
        time.sleep(0.5)
        # The durable state AT that transition, from the production writers:
        if transition == "before-claim":
            check("before-claim: probed, nothing claimed, nothing delivered, nothing attributed",
                  not claimed(w, "task-p.txt") and w.sentinels("task-p") == [] and w.attribution("task-p") is None,
                  f"claimed={claimed(w, 'task-p.txt')} sentinels={w.sentinels('task-p')}")
        else:
            check(f"{transition}: claimed by this watcher, delivered to A and attributed",
                  w.claim_pid("task-p.txt") == str(w1.proc.pid)
                  and w.sentinels("task-p") == [f"deliveries/{A}/task-p.txt"] and w.attribution("task-p") == A,
                  f"claim={w.claim_pid('task-p.txt')} sentinels={w.sentinels('task-p')} attr={w.attribution('task-p')}")
        w1.stop(graceful=True)
        if delivered_before_kill:
            expect_left_behind(w, w1, "task-p.txt", True)
        else:
            check("the dying watcher announced NO TASK_FILE for it", w1.announced("task-p.txt") == 0)
            check("nothing was claimed or delivered before the kill",
                  not claimed(w, "task-p.txt") and w.sentinels("task-p") == [], str(w.sentinels("task-p")))
        w.pause_at("")
        w2 = Watcher(w)
        w2.start()
        expect_settled(w, w2, "task-p.txt", runs=2 if delivered_before_kill else 1)
        check("the router ran to completion exactly once after the kill",
              router_runs(w) == (2 if delivered_before_kill else 1), str(w.log()))
    finally:
        w1.stop()
        if w2 is not None:
            w2.stop()
        w.cleanup()


def arm_during_routing(sub: str, mode: str) -> None:
    print(f"\narm {sub}: TERM while the router is in flight ({mode})")
    w = Workspace()
    w.mode(mode)
    w1 = Watcher(w)
    w2 = None
    try:
        w1.start()
        w1.deliver("task-b.txt")
        check("the handler is running when the TERM lands",
              wait_for(lambda: any(ln.startswith("run ") for ln in w.log()), 30.0))
        if mode == "hang-after":
            check("...and the router had delivered",
                  wait_for(lambda: w.sentinels("task-b") == [f"deliveries/{A}/task-b.txt"], 30.0))
        time.sleep(0.5)
        w1.stop(graceful=True)
        expect_left_behind(w, w1, "task-b.txt", mode == "hang-after")
        w.mode("normal")
        w2 = Watcher(w)
        w2.start()
        expect_settled(w, w2, "task-b.txt", runs=2 if mode == "hang-after" else 1)
    finally:
        w1.stop()
        if w2 is not None:
            w2.stop()
        w.cleanup()


def proc_start(pid: int) -> str:
    return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.strip()


def seed_delivered(w: Workspace, name: str) -> Path:
    """The state a kill after delivery leaves: sentinel + attribution, task in tasks/."""
    tid = name[:-4]
    task = w.task(name)
    (w.ws / "deliveries" / A).mkdir(parents=True, exist_ok=True)
    (w.ws / "deliveries" / A / f"{tid}.txt").touch()
    pa.record(w.ws, tid, A)
    w.claims.mkdir(parents=True, exist_ok=True)
    return task


def arm_recycled_pid() -> None:
    print("\narm e1: the left-behind claim names a pid that was recycled by another process")
    w = Workspace()
    task = seed_delivered(w, "task-e.txt")
    holder = subprocess.Popen(["sleep", "100000"])       # a live pid that is NOT the owner
    (w.claims / "task-e.txt").write_text(
        f"{holder.pid}\nsome-dead-watcher\n{task}\nfallback\nSat Jan  1 00:00:00 2000\n")
    w2 = Watcher(w)
    try:
        w2.start()
        expect_settled(w, w2, "task-e.txt", runs=1)
    finally:
        w2.stop()
        holder.kill()
        holder.wait()
        w.cleanup()


def arm_live_owner_preserved() -> None:
    print("\narm e2: the claim names a live pid WITH its true start time — a live owner, left alone")
    w = Workspace()
    task = seed_delivered(w, "task-f.txt")
    holder = subprocess.Popen(["sleep", "100000"])
    claim = w.claims / "task-f.txt"
    claim.write_text(f"{holder.pid}\nsome-live-watcher\n{task}\nfallback\n{proc_start(holder.pid)}\n")
    before = claim.read_text()
    w2 = Watcher(w)
    try:
        w2.start()
        poke(w, w2, "task-q.txt")
        check("the live owner's claim is preserved untouched",
              claim.is_file() and claim.read_text() == before,
              claim.read_text()[:80] if claim.is_file() else "retired")
        check("and the router is NOT run for the task under another watcher's live claim",
              w.sentinels("task-f") == [f"deliveries/{A}/task-f.txt"] and router_runs(w) == 1, str(w.log()))
    finally:
        w2.stop()
        holder.kill()
        holder.wait()
        w.cleanup()


def arm_production_writer_identity_across_locales() -> None:
    print("\narm e3: the production writer's claim, read by a watcher under another locale")
    w = Workspace()
    w.pause_at("after-handler")
    w1 = Watcher(w)
    w2 = None
    try:
        w1.start()
        w1.deliver("task-w.txt")
        check("the first watcher is paused with its claim held",
              wait_for(lambda: in_hook(w, "after-handler") and claimed(w, "task-w.txt"), 30.0))
        lines = (w.claims / "task-w.txt").read_text().splitlines()
        expected = subprocess.run(["ps", "-o", "lstart=", "-p", str(w1.proc.pid)], capture_output=True, text=True,
                                  env={**os.environ, "LC_ALL": "C"}).stdout.strip()
        check("the production writer wrote the owner's start time as the fifth line, in the C locale",
              len(lines) >= 5 and lines[4] == expected and expected != "", str(lines))
        # A second watcher under a different locale must still see a LIVE owner and leave it alone.
        w.pause_at("")
        w2 = Watcher(w, role="session")
        w2.start(extra_env={"LC_ALL": "fr_FR.UTF-8", "LANG": "fr_FR.UTF-8"})
        poke(w, w2, "task-q.txt")
        check("a reader under another locale keeps the live owner's claim",
              claimed(w, "task-w.txt") and w.claim_pid("task-w.txt") == str(w1.proc.pid), w.claim_pid("task-w.txt"))
        check("...and does not run the router concurrently for it", router_runs(w) == 2, str(w.log()))
    finally:
        if w2 is not None:
            w2.stop()
        w1.stop()
        w.cleanup()


def arm_zombie_owner_is_retired() -> None:
    print("\narm e4: the dead watcher is left a zombie — alive to kill -0, its production claim retired")
    w = Workspace()
    w.mode("hang-before")
    w1 = Watcher(w)
    w2 = None
    try:
        w1.start()
        w1.deliver("task-z.txt")
        check("the handler is running when the TERM lands",
              wait_for(lambda: any(ln.startswith("run ") for ln in w.log()), 30.0))
        claim = w.claims / "task-z.txt"
        lines = claim.read_text().splitlines() if claim.is_file() else []
        check("the production writer's claim has five lines", len(lines) == 5 and lines[4] != "", str(lines))
        os.killpg(os.getpgid(w1.proc.pid), signal.SIGTERM)
        time.sleep(2.0)     # exited, NOT reaped: a zombie
        check("the zombie still answers kill -0", os.kill(w1.proc.pid, 0) is None)
        w.mode("normal")
        w2 = Watcher(w)
        w2.start()
        expect_settled(w, w2, "task-z.txt", runs=1)
    finally:
        if w2 is not None:
            w2.stop()
        w1.stop()
        w.cleanup()


def arm_reader_ps_failure_keeps_then_recovers() -> None:
    print("\narm e5: the reader's ps fails — a live owner's claim is kept and named; once ps answers, a dead one is retired")
    w = Workspace()
    w.pause_at("after-handler")
    w1 = Watcher(w)
    w2 = None
    broken_ps = w.tmp / "bin" / "ps"
    try:
        w1.start()
        w1.deliver("task-o.txt")
        check("the first watcher is paused with its claim held",
              wait_for(lambda: in_hook(w, "after-handler") and claimed(w, "task-o.txt"), 30.0))
        w.pause_at("")
        broken_ps.write_text("#!/bin/sh\nexit 1\n")
        broken_ps.chmod(0o755)
        w2 = Watcher(w, role="session")
        w2.start()
        # With ps failing the writer refuses to claim anything (fail closed), so a
        # routed poke cannot be the signal here; the reader's own diagnostic is.
        check("the reader says why it keeps the claim",
              wait_for(lambda: "cannot read the start time" in w2.err.read_text(), 30.0), w2.err.read_text()[-300:])
        check("with ps failing, the live owner's claim is kept",
              w.claim_pid("task-o.txt") == str(w1.proc.pid), w.claim_pid("task-o.txt"))
        check("...and the writer refused to claim rather than publish a blank identity",
              wait_for(lambda: "cannot read my own start time" in w2.err.read_text(), 30.0), w2.err.read_text()[-300:])
        check("...so no claim of w2's exists anywhere", all(
              (w.claims / c.name).read_text().splitlines()[0] != str(w2.proc.pid) for c in w.claims.glob("task-*.txt")))
        check("...and the router did not run for it concurrently",
              w.sentinels("task-o") == [f"deliveries/{A}/task-o.txt"] and router_runs(w) == 1, str(w.log()))
        broken_ps.unlink()                 # observation recovers; the owner is still alive
        poke(w, w2, "task-q2.txt")
        check("with ps back, the live owner's claim is still kept", w.claim_pid("task-o.txt") == str(w1.proc.pid))
        w1.stop(graceful=True)             # now the owner dies; the next dispatch re-asks and sees it
        poke(w, w2, "task-q3.txt")
        expect_settled(w, w2, "task-o.txt", runs=4)
    finally:
        if w2 is not None:
            w2.stop()
        w1.stop()
        w.cleanup()


def arm_legacy_four_line_claim() -> None:
    print("\narm e6: a four-line claim from the previous writer is read by pid alone")
    w = Workspace()
    task = seed_delivered(w, "task-l.txt")
    holder = subprocess.Popen(["sleep", "100000"])
    claim = w.claims / "task-l.txt"
    claim.write_text(f"{holder.pid}\nsome-live-watcher\n{task}\nfallback\n")
    w2 = Watcher(w)
    try:
        w2.start()
        poke(w, w2, "task-q.txt")
        check("a live pid with no fifth line is a live owner", claimed(w, "task-l.txt") and router_runs(w) == 1,
              str(w.log()))
        holder.kill()
        holder.wait()
        poke(w, w2, "task-q2.txt")         # the next dispatch re-asks and sees a dead pid
        expect_settled(w, w2, "task-l.txt", runs=3)
    finally:
        w2.stop()
        if holder.poll() is None:
            holder.kill()
            holder.wait()
        w.cleanup()


def notifier_guard(runtime: str) -> str:
    """The production `filename_is_claimed` of one notifier, extracted verbatim."""
    src = (REPO / "src" / "agent" / runtime / "cli" / "task-notifier.sh").read_text()
    start = src.index("filename_is_claimed() {")
    return src[start:src.index("\n}\n", start) + 3]


def guards_say_claimed(w: Workspace, name: str) -> dict:
    out = {}
    for runtime in ("claude", "codex"):
        r = subprocess.run(["bash", "-c", notifier_guard(runtime) + f'\nfilename_is_claimed "{name}"'],
                           env={"CLAIMS_DIR": str(w.claims), "PATH": os.environ["PATH"]}, capture_output=True)
        out[runtime] = r.returncode == 0
    return out


def arm_no_handler_restart() -> None:
    print("\narm f: the next watcher has no handler declared — a handler's dead claim is held, never announced; the returning handler replays it once")
    w = Workspace()
    w.mode("hang-before")
    w1 = Watcher(w)
    w2 = None
    try:
        w1.start()
        w1.deliver("task-n.txt")
        check("the handler is running when the TERM lands",
              wait_for(lambda: any(ln.startswith("run ") for ln in w.log()), 30.0))
        w1.stop(graceful=True)
        expect_left_behind(w, w1, "task-n.txt", False)
        check("both production notifier guards would DROP the task while that claim stands",
              guards_say_claimed(w, "task-n.txt") == {"claude": True, "codex": True}, str(guards_say_claimed(w, "task-n.txt")))
        w2 = Watcher(w, handler=False)
        w2.start()
        check("the no-handler watcher holds it: a handler's outcome is unknown and no handler is here",
              wait_for(lambda: "no handler is available" in w2.err.read_text(), 30.0), w2.err.read_text()[-300:])
        time.sleep(7.0)      # three held-retry intervals
        check("...zero TASK_FILE lines, the dead claim preserved, nothing delivered",
              w2.announced("task-n.txt") == 0 and claimed(w, "task-n.txt") and w.sentinels("task-n") == [],
              w2.out.read_text()[-300:])
        w.mode("normal")
        (w.ws / "state" / "task-event-handler.json").write_text(json.dumps({"handler": str(w.handler)}))
        check("the returning handler replays it exactly once and releases the claim",
              wait_for(lambda: w.sentinels("task-n") == [f"deliveries/{A}/task-n.txt"] and router_runs(w) == 1
                       and not claimed(w, "task-n.txt"), 40.0), f"{w.sentinels('task-n')} {w.log()}")
        time.sleep(5.0)
        check("...and it is never announced to the core", w2.announced("task-n.txt") == 0 and router_runs(w) == 1, str(w.log()))
    finally:
        w1.stop()
        if w2 is not None:
            w2.stop()
        w.cleanup()


def arm_no_handler_live_claim_committed_never_announced() -> None:
    print("\narm h: no-handler watcher, a live claim it cannot identify on a task ALREADY delivered — hold; owner dies — still never announced")
    w = Workspace()
    w.pause_at("after-handler")
    w1 = Watcher(w)
    w2 = None
    broken_ps = w.tmp / "bin" / "ps"
    try:
        w1.start()
        w1.deliver("task-h.txt")
        check("the owner is paused with its production claim held, delivery committed to A",
              wait_for(lambda: in_hook(w, "after-handler") and claimed(w, "task-h.txt"), 30.0)
              and w.sentinels("task-h") == [f"deliveries/{A}/task-h.txt"] and w.attribution("task-h") == A)
        w.pause_at("")
        broken_ps.write_text("#!/bin/sh\nexit 1\n")
        broken_ps.chmod(0o755)
        w2 = Watcher(w, handler=False, role="session")
        w2.start()
        check("the no-handler watcher holds the task rather than announcing into the guards",
              wait_for(lambda: "no handler is available" in w2.err.read_text(), 30.0), w2.err.read_text()[-300:])
        time.sleep(3.0)
        check("zero TASK_FILE lines while the claim stands", w2.announced("task-h.txt") == 0, w2.out.read_text()[-200:])
        broken_ps.unlink()               # ps returns; the owner is still alive
        time.sleep(5.0)
        check("still zero with ps back and the owner alive", w2.announced("task-h.txt") == 0, w2.out.read_text()[-200:])
        w1.stop(graceful=True)           # the owner dies; nothing else is fed
        time.sleep(7.0)                  # three retry intervals with no handler available
        check("with no handler here the dead claim is preserved, not retired", claimed(w, "task-h.txt"), w.claim_pid("task-h.txt"))
        check("...and the task is held, never announced to the core", w2.announced("task-h.txt") == 0
              and "no handler is available" in w2.err.read_text(), w2.out.read_text()[-300:])
        check("A's sentinel and attribution are untouched",
              w.sentinels("task-h") == [f"deliveries/{A}/task-h.txt"] and w.attribution("task-h") == A)
        # Restart durability: the holding watcher dies; a fresh no-handler watcher must
        # recover the same state from the claim on disk, not from the old HELD_NAMES.
        w2.stop()
        w3 = Watcher(w, handler=False)
        w3.start()
        check("a fresh no-handler watcher holds it again from the durable claim",
              wait_for(lambda: "no handler is available" in w3.err.read_text(), 30.0), w3.err.read_text()[-300:])
        time.sleep(5.0)
        check("...zero core lines, no handler run, claim still there",
              w3.announced("task-h.txt") == 0 and router_runs(w) == 1 and claimed(w, "task-h.txt"), str(w.log()))
        # The handler returns (its declaration appears): the held task is replayed through it.
        (w.ws / "state" / "task-event-handler.json").write_text(json.dumps({"handler": str(w.handler)}))
        check("the returned handler replays it: the dead claim is retired and released",
              wait_for(lambda: router_runs(w) == 2 and not claimed(w, "task-h.txt"), 40.0), f"{w.log()} {w.claim_pid('task-h.txt')}")
        check("...as 'already': still one sentinel, attribution A, and still never announced",
              w.sentinels("task-h") == [f"deliveries/{A}/task-h.txt"] and w.attribution("task-h") == A
              and w3.announced("task-h.txt") == 0, str(w.sentinels("task-h")))
        w3.stop()
    finally:
        if w2 is not None:
            w2.stop()
        w1.stop()
        w.cleanup()


def arm_no_handler_live_claim_uncommitted_announced_once() -> None:
    print("\narm h2: the same, but the owner died BEFORE delivering — held until a handler returns, then routed once")
    w = Workspace()
    w.mode("hang-before")
    w1 = Watcher(w)
    w2 = None
    broken_ps = w.tmp / "bin" / "ps"
    try:
        w1.start()
        w1.deliver("task-u.txt")
        check("the owner holds its production claim, nothing delivered",
              wait_for(lambda: any(ln.startswith("run ") for ln in w.log()) and claimed(w, "task-u.txt"), 30.0)
              and w.sentinels("task-u") == [])
        broken_ps.write_text("#!/bin/sh\nexit 1\n")
        broken_ps.chmod(0o755)
        w2 = Watcher(w, handler=False, role="session")
        w2.start()
        check("held: a handler's claim stands and no handler is here",
              wait_for(lambda: "no handler is available" in w2.err.read_text(), 30.0), w2.err.read_text()[-300:])
        time.sleep(3.0)
        check("zero TASK_FILE lines while the claim stands", w2.announced("task-u.txt") == 0)
        broken_ps.unlink()
        w1.stop(graceful=True)           # the owner dies before delivering; nothing else is fed
        time.sleep(7.0)
        check("with no handler here the dead claim is preserved and the task never announced",
              claimed(w, "task-u.txt") and w2.announced("task-u.txt") == 0, w2.out.read_text()[-300:])
        w2.stop()                        # restart durability: a successor recovers from the claim on disk
        w3 = Watcher(w, handler=False)
        w3.start()
        check("a fresh no-handler watcher holds it again from the durable claim",
              wait_for(lambda: "no handler is available" in w3.err.read_text(), 30.0), w3.err.read_text()[-300:])
        time.sleep(5.0)
        check("...zero core lines, no handler run, claim still there",
              w3.announced("task-u.txt") == 0 and router_runs(w) == 0 and claimed(w, "task-u.txt"), str(w.log()))
        w.mode("normal")
        (w.ws / "state" / "task-event-handler.json").write_text(json.dumps({"handler": str(w.handler)}))
        check("the returned handler replays it: routed exactly once, claim released",
              wait_for(lambda: w.sentinels("task-u") == [f"deliveries/{A}/task-u.txt"] and router_runs(w) == 1
                       and not claimed(w, "task-u.txt"), 40.0), f"{w.sentinels('task-u')} {w.log()}")
        time.sleep(5.0)
        check("...and never announced to the core, never routed twice",
              w3.announced("task-u.txt") == 0 and router_runs(w) == 1, str(w.log()))
        w3.stop()
    finally:
        if w2 is not None:
            w2.stop()
        w1.stop()
        w.cleanup()


def arm_handler_overlap_lost_acquisition_recovers(sig: str = "TERM") -> None:
    print(f"\narm l ({sig}): a handler-enabled session watcher sweeps while the standby owner is alive (claim lost, rc 1); the owner dies; quiet inbox")
    w = Workspace()
    w.mode("hang-before")
    w1 = Watcher(w)
    w2 = None
    try:
        w1.start()
        w1.deliver("task-v.txt")
        check("the standby owner holds its claim, nothing delivered",
              wait_for(lambda: any(ln.startswith("run ") for ln in w.log()) and claimed(w, "task-v.txt"), 30.0)
              and w.sentinels("task-v") == [])
        w.mode("normal")
        w2 = Watcher(w, role="session")
        w2.start()
        time.sleep(8.0)                  # its sweep met the live claim
        check("the session watcher did not route it while the owner lived (several timer passes)",
              router_runs(w) == 0 and w.claim_pid("task-v.txt") == str(w1.proc.pid), str(w.log()))
        if sig == "KILL":
            os.killpg(os.getpgid(w1.proc.pid), signal.SIGKILL)   # no settle, no log line
            w1.proc.wait(timeout=5)
        else:
            w1.stop(graceful=True)       # the owner dies; nothing else is fed
        check("the session watcher routes it exactly once on its own timer",
              wait_for(lambda: w.sentinels("task-v") == [f"deliveries/{A}/task-v.txt"] and router_runs(w) == 1, 30.0),
              f"{w.sentinels('task-v')} {w.log()}")
        check("...and releases its claim", wait_for(lambda: not claimed(w, "task-v.txt"), 30.0), w.claim_pid("task-v.txt"))
        time.sleep(5.0)
        check("...and never a second time", router_runs(w) == 1 and w2.announced("task-v.txt") == 0, str(w.log()))
    finally:
        if w2 is not None:
            w2.stop()
        w1.stop()
        w.cleanup()


def arm_dead_claim_met_by_own_dispatch_once() -> None:
    print("\narm i: a no-handler watcher's own dispatch meets a dead handler claim — held, never announced; the returning handler replays it once")
    w = Workspace()
    w2 = Watcher(w, handler=False)
    try:
        w2.start()
        wait_for(lambda: not list((w.ws / "state").glob("watch-tasks-stream.start-*.lock")), 20.0)
        task = w.task("task-i.txt")
        w.claims.mkdir(parents=True, exist_ok=True)
        (w.claims / "task-i.txt").write_text(f"{dead_pid()}\nsome-dead-watcher\n{task}\nfallback\n\n")
        with w2.feed.open("a") as fh:            # the task's own event, claim already dead
            fh.write(str(task.resolve()) + "\n")
        check("held, with the reason logged", wait_for(lambda: "no handler is available" in w2.err.read_text(), 30.0),
              w2.err.read_text()[-300:])
        time.sleep(7.0)                          # three held-retry intervals
        check("zero announcements, the dead claim preserved (a handler's outcome is unknown)",
              w2.announced("task-i.txt") == 0 and claimed(w, "task-i.txt"), w2.out.read_text()[-300:])
        (w.ws / "state" / "task-event-handler.json").write_text(json.dumps({"handler": str(w.handler)}))
        check("the returning handler retires the dead claim and routes it exactly once",
              wait_for(lambda: w.sentinels("task-i") == [f"deliveries/{A}/task-i.txt"] and router_runs(w) == 1
                       and not claimed(w, "task-i.txt"), 40.0), f"{w.sentinels('task-i')} {w.log()}")
        time.sleep(5.0)
        check("...once, and never announced", router_runs(w) == 1 and w2.announced("task-i.txt") == 0, str(w.log()))
    finally:
        w2.stop()
        w.cleanup()


def dead_pid() -> str:
    p = subprocess.Popen(["sh", "-c", "true"])
    p.wait()
    return str(p.pid)


def arm_eof_while_held_exits_promptly() -> None:
    print("\narm k: fswatch closes while a task is held — genuine EOF still ends the watcher promptly")
    w = Workspace()
    broken_ps = w.tmp / "bin" / "ps"
    broken_ps.write_text("#!/bin/sh\nexit 1\n")
    broken_ps.chmod(0o755)
    w2 = Watcher(w)
    try:
        w2.start()
        w2.deliver("task-k.txt")
        check("the task is held (writer refused)",
              wait_for(lambda: "cannot read my own start time" in w2.err.read_text(), 30.0), w2.err.read_text()[-300:])
        # Close the stub fswatch (the sh wrapper or the `tail -f` it execs): the
        # watcher's fd 3 sees EOF. Walk its process group, since the stub's name changes.
        pg = os.getpgid(w2.proc.pid)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 10 and w2.proc.poll() is None:
            for ln in subprocess.run(["ps", "-o", "pid=,pgid=,command=", "-ax"], capture_output=True, text=True).stdout.splitlines():
                parts = ln.split(None, 2)
                if len(parts) == 3 and parts[1] == str(pg) and parts[0] != str(w2.proc.pid) \
                        and ("fswatch" in parts[2] or "tail -n +1" in parts[2]):
                    try:
                        os.kill(int(parts[0]), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            time.sleep(0.25)
        check("the watcher exits on EOF within a few seconds, not a retry loop",
              w2.proc.poll() is not None, f"still running after {time.monotonic()-t0:.1f}s")
        check("...and it announced nothing for the held task", w2.announced("task-k.txt") == 0)
    finally:
        w2.stop()
        w.cleanup()


def arm_writer_ps_failure_holds_then_routes_once() -> None:
    print("\narm j: the writer's ps fails on a task nobody holds — refused, held, routed once after ps returns")
    w = Workspace()
    broken_ps = w.tmp / "bin" / "ps"
    broken_ps.write_text("#!/bin/sh\nexit 1\n")
    broken_ps.chmod(0o755)
    w2 = Watcher(w)
    try:
        w2.start()
        w2.deliver("task-j.txt")
        check("the writer refuses to publish a blank identity",
              wait_for(lambda: "cannot read my own start time" in w2.err.read_text(), 30.0), w2.err.read_text()[-300:])
        time.sleep(3.0)
        check("nothing claimed, nothing routed, nothing announced",
              not claimed(w, "task-j.txt") and w.sentinels("task-j") == [] and w2.announced("task-j.txt") == 0,
              f"{claimed(w, 'task-j.txt')} {w.sentinels('task-j')}")
        broken_ps.unlink()               # ps returns; nothing else is fed
        check("the task is routed exactly once on the watcher's own timer",
              wait_for(lambda: w.sentinels("task-j") == [f"deliveries/{A}/task-j.txt"] and router_runs(w) == 1, 30.0),
              f"{w.sentinels('task-j')} {w.log()}")
        check("...and its claim is released", wait_for(lambda: not claimed(w, "task-j.txt"), 30.0))
        time.sleep(5.0)
        check("...and it is not routed again", router_runs(w) == 1 and w2.announced("task-j.txt") == 0, str(w.log()))
    finally:
        w2.stop()
        w.cleanup()


def arm_rebind_between_kill_and_replay() -> None:
    print("\narm g: delivered to A, watcher killed, room rebound to B, replayed")
    w = Workspace()
    w.mode("hang-after")
    w1 = Watcher(w)
    w2 = None
    try:
        w1.start()
        w1.deliver("task-g.txt")
        check("the router delivered to A", wait_for(lambda: w.sentinels("task-g") == [f"deliveries/{A}/task-g.txt"], 30.0))
        time.sleep(0.5)
        w1.stop(graceful=True)
        expect_left_behind(w, w1, "task-g.txt", True)
        w.bind(B)
        w.mode("normal")
        w2 = Watcher(w)
        w2.start()
        expect_settled(w, w2, "task-g.txt", runs=2, recipient=A)
        check("B never received a sentinel", not (w.ws / "deliveries" / B / "task-g.txt").exists())
    finally:
        w1.stop()
        if w2 is not None:
            w2.stop()
        w.cleanup()


def main() -> int:
    arm_barrier("a", "before-claim", delivered_before_kill=False)
    arm_during_routing("b1", "hang-before")
    arm_during_routing("b2", "hang-after")
    arm_barrier("c", "after-handler", delivered_before_kill=True)
    arm_barrier("d", "after-done", delivered_before_kill=True)
    arm_recycled_pid()
    arm_live_owner_preserved()
    arm_production_writer_identity_across_locales()
    arm_zombie_owner_is_retired()
    arm_reader_ps_failure_keeps_then_recovers()
    arm_legacy_four_line_claim()
    arm_no_handler_restart()
    arm_no_handler_live_claim_committed_never_announced()
    arm_no_handler_live_claim_uncommitted_announced_once()
    arm_handler_overlap_lost_acquisition_recovers("TERM")
    arm_handler_overlap_lost_acquisition_recovers("KILL")
    arm_dead_claim_met_by_own_dispatch_once()
    arm_writer_ps_failure_holds_then_routes_once()
    arm_eof_while_held_exits_promptly()
    arm_rebind_between_kill_and_replay()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nPASS — a kill at any phase ends with one sentinel, one attribution, no announce, "
          "no refusal, and no claim left behind; a recycled pid, a no-handler restart and a "
          "rebind are each recovered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
