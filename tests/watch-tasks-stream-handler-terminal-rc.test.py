#!/usr/bin/env python3
"""A handler's own terminal exit code outranks the disposition fixed at probe time.

The disposition is recorded when the task is ADMITTED (`acquire_task_claim`, from
the probe's verdict) and `finish_handler_task` branched on that stored value
alone. So a handler that probes 0 -- "I accept this" -- and then fails its real
run had that failure read as "optional handler declined", and the task was
emitted to the unrestricted live core.

Exit 4 is the protocol's "must-handle": the handler saying the core must not
inherit this work. `pool_route_handler.py` returns it from every real-run failure
for exactly that reason, and until this fix the watcher ignored it.

Synchronisation: every scenario ends on something the watcher itself writes --
the TASK_FILE line, the published result, or the claim's release after the
`after-done` transition -- never on a window elapsing. The deadline is a bound a
stall reaches, not the expected cost. A watcher that exits early ends its wait
at once, and a FAIL prints where it stopped: the transitions it reached (via
the documented `SUTANDO_WATCHER_TRANSITION_HOOK` seam), its exit status, the
claim and result state, and its stderr.

Run: python3 tests/watch-tasks-stream-handler-terminal-rc.test.py
"""
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from clean_watcher_env import clean_env  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []
# A bound only: the normal cost of every wait below is a second or two.
DEADLINE_S = 40.0
# Read-only tail after a task is settled: an emit after the claim's release is a defect too.
TAIL_S = 2.5
HOOK = '#!/bin/sh\nprintf "%s %s %s\\n" "$(date +%s)" "$1" "$2" >> "$TRANSITIONS"\n'


class Workspace:
    """One synthetic workspace: stub fswatch (never fires), a handler that probes
    `probe_rc` then exits `real_run_rc`, and the transition log."""

    def __init__(self, prefix: str, real_run_rc: int, probe_rc: int = 0) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix=prefix))
        atexit.register(shutil.rmtree, self.tmp, True)
        self.ws = self.tmp / "ws"
        (self.ws / "tasks").mkdir(parents=True)
        (self.ws / "results" / "archive").mkdir(parents=True)
        (self.ws / "state").mkdir()
        feed = self.tmp / "feed"
        feed.write_text("")
        b = self.tmp / "bin"
        b.mkdir()
        (b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n")
        (b / "fswatch").chmod(0o755)
        self.handler = self.tmp / "handler.sh"
        self.handler.write_text('#!/bin/sh\n'
                                f'for a in "$@"; do [ "$a" = "--probe" ] && exit {probe_rc}; done\n'
                                f'exit {real_run_rc}\n')
        self.handler.chmod(0o755)
        self.hook = self.tmp / "hook.sh"
        self.hook.write_text(HOOK)
        self.hook.chmod(0o755)
        self.transitions = self.tmp / "transitions.log"
        self.claims = self.ws / "state" / "task-event-handler-claims"
        self.generation = 0

    def task(self, tid: str) -> None:
        (self.ws / "tasks" / f"{tid}.txt").write_text(f"id: {tid}\naccess_tier: owner\ntask: probe\n")

    def env(self) -> dict:
        env = clean_env()
        env["PATH"] = f"{self.tmp / 'bin'}:{env['PATH']}"
        env["TMPDIR"] = str(self.tmp)
        env["SUTANDO_RESULTS_DIR"] = str(self.ws / "results")
        env["SUTANDO_TASK_EVENT_HANDLER"] = str(self.handler)
        env["SUTANDO_WATCHER_TRANSITION_HOOK"] = str(self.hook)
        env["TRANSITIONS"] = str(self.transitions)
        return env

    def start(self) -> "Watcher":
        self.generation += 1
        # stderr is kept: a FAIL with nothing to read cannot be diagnosed.
        errf = open(self.tmp / f"watcher-{self.generation}.err", "w+")
        p = subprocess.Popen(["bash", "src/watch-tasks-stream.sh", str(self.ws / "tasks"),
                              "--role", "standby", "--inbox", str(self.ws / "tasks")],
                             cwd=str(REPO), env=self.env(), stdout=subprocess.PIPE,
                             stderr=errf, start_new_session=True)
        os.set_blocking(p.stdout.fileno(), False)
        return Watcher(self, p, errf)

    def published(self) -> list[str]:
        return sorted(q.name for q in (self.ws / "results").glob("*.txt"))

    def claimed(self, tid: str) -> bool:
        return (self.claims / f"{tid}.txt").exists()

    def sentinel_pids(self) -> list[str]:
        return [q.read_text().strip() for q in (self.ws / "state").glob("watch-tasks-stream*.pid")]

    def transitions_seen(self) -> list[str]:
        return self.transitions.read_text().splitlines() if self.transitions.exists() else []

    def reached(self, transition: str) -> bool:
        return any(ln.split(" ")[1:2] == [transition] for ln in self.transitions_seen())


class Watcher:
    def __init__(self, ws: Workspace, p: subprocess.Popen, errf) -> None:
        self.ws, self.p, self.errf = ws, p, errf
        self.out = b""
        self.waits: list[str] = []  # how each wait ended: seen | exited | timeout
        self.stderr = ""

    def pump(self) -> None:
        try:
            chunk = os.read(self.p.stdout.fileno(), 65536)
        except BlockingIOError:
            return
        except OSError:
            return
        if chunk:
            self.out += chunk

    @property
    def emitted(self) -> bool:
        return b"TASK_FILE" in self.out

    def wait_until(self, pred, deadline: float = DEADLINE_S, step: float = 0.1) -> bool:
        """Poll `pred` until it holds, the watcher exits, or `deadline` elapses."""
        started = time.time()
        end = started + deadline

        def done(how: str, result: bool) -> bool:
            self.waits.append(f"{how} after {time.time() - started:.1f}s")
            return result

        while True:
            self.pump()
            if pred():
                return done("seen", True)
            if self.p.poll() is not None:
                self.pump()
                return done("exited", pred())
            if time.time() >= end:
                return done("timeout", False)
            time.sleep(step)

    def tail(self, since: float, seconds: float = TAIL_S, step: float = 0.1) -> None:
        """Keep reading stdout until `seconds` after `since` (the task settled), so an
        emit that lands after the claim's release is still caught; ends with the watcher."""
        end = since + seconds
        while time.time() < end and self.p.poll() is None:
            time.sleep(step)
            self.pump()
        self.pump()

    def stop(self) -> None:
        try:
            os.killpg(os.getpgid(self.p.pid), 15)
        except Exception:
            pass
        try:
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()
            self.p.wait(timeout=5)
        self.pump()
        self.errf.seek(0)
        self.stderr = self.errf.read()
        self.errf.close()

    def diagnosis(self) -> str:
        lines = [f"waits ended by: {self.waits}; watcher exit status: {self.p.returncode}",
                 f"transitions: {self.ws.transitions_seen()}",
                 f"claims held: {sorted(q.name for q in self.ws.claims.glob('task-*')) if self.ws.claims.exists() else []}",
                 f"results: {self.ws.published()}",
                 f"stdout: {self.out!r}"]
        err = self.stderr.strip().splitlines()[-20:]
        if err:
            lines.append("watcher stderr:")
            lines.extend("  " + ln for ln in err)
        return "\n".join("  " + ln for ln in lines)


LAST_DIAG = [""]


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
        if LAST_DIAG[0]:
            print(LAST_DIAG[0])


def start(real_run_rc: int, probe_rc: int = 0):
    """One pre-existing task under a real watcher; scenarios start together so
    their tails overlap instead of adding up."""
    ws = Workspace("term-rc-", real_run_rc, probe_rc)
    ws.task("task-demo")
    return ws, ws.start()


def finish(ws: Workspace, w: Watcher, settled, tail: bool = True) -> tuple[bool, list[str], bool]:
    """Wait for `settled(ws, w)`, the watcher-written observable that ends the
    scenario. Returns (emitted, published, settled-as-named). An absence check
    keeps reading for TAIL_S after the task settled."""
    # A claim released after the handler ran means the watcher is done with the
    # task whatever it decided: a wrong outcome is diagnosed now, not at the bound.

    def over() -> bool:
        return ws.reached("after-handler") and not ws.claimed("task-demo")

    w.wait_until(lambda: settled(ws, w) or over())
    # Let the watcher settle its own claim before it is stopped, so a normal
    # run leaves no in-flight claim behind to muddy the shutdown lines.
    w.wait_until(lambda: not ws.claimed("task-demo"), deadline=10.0)
    settled_at = time.time()
    reached = settled(ws, w)
    if tail:
        w.tail(settled_at)
    w.stop()
    LAST_DIAG[0] = w.diagnosis()
    return w.emitted, ws.published(), reached


def restart_witness(ws: Workspace, first: Watcher):
    """REVIEW.md 15 for this change: a watcher that is STOPPED and STARTED AGAIN
    takes one probe-0/rc-4 task through to a published terminal failure.

    The watcher is a real process both times -- the same `src/watch-tasks-stream.sh`
    the core runs -- so what is exercised is the shipped path across a restart
    boundary, not a harness standing in for it. Only the workspace and the
    fswatch trigger are synthetic.
    """
    # Up means stamped: the sentinel names this pid only once fswatch is confirmed running.
    came_up = first.wait_until(lambda: str(first.p.pid) in ws.sentinel_pids())
    first_pid = first.p.pid
    first.stop()                         # THE RESTART BOUNDARY
    LAST_DIAG[0] = first.diagnosis()
    check("restart: the first watcher came up before it was stopped", came_up)

    # Written while NO watcher runs, so the restarted process admits it on its own
    # startup sweep; created later, the stub fswatch never fires and nothing runs.
    ws.task("task-restart")
    second = ws.start()
    second.wait_until(lambda: ws.published() != []
                      or (ws.reached("after-handler") and not ws.claimed("task-restart")))
    second.wait_until(lambda: not ws.claimed("task-restart"), deadline=10.0)
    settled_r = ws.reached("after-handler") and not ws.claimed("task-restart")
    second.tail(time.time())
    second.stop()
    LAST_DIAG[0] = second.diagnosis()
    published = ws.published()
    body = (ws.ws / "results" / published[0]).read_text(errors="replace")[:200] if published else ""
    return first_pid, second.p.pid, second.emitted, published, body, settled_r


must_handle, ordinary, success = start(4), start(1), start(0)
restart_ws = Workspace("term-rc-restart-", real_run_rc=4)
restart_first = restart_ws.start()
emitted, published, settled_four = finish(
    *must_handle, settled=lambda ws, w: ws.reached("after-handler") and not ws.claimed("task-demo"))
check("a must-handle task settles: the handler ran and the claim was released", settled_four,
      "the watcher never finished the task, so 'not emitted' would be vacuous")
check("a must-handle result is NOT emitted to the live core", not emitted,
      "the task reached the unrestricted core despite the handler refusing it")
check("a must-handle result publishes a terminal failure instead", published != [],
      "nothing was published, so the task is neither delivered nor failed")

# Control: treating EVERY failure as must-handle would pass the case above while
# removing the fallback feature, so an ordinary rc=1 must still reach the core.
emitted_one, _, _ = finish(*ordinary, settled=lambda ws, w: w.emitted, tail=False)
check("control: an ordinary failure still falls back to the core", emitted_one,
      "the fallback path was removed, not narrowed")

# Control 2: success is not a failure. Without this the first check passes for a
# watcher that never emits anything at all. A success is over once the watcher
# has recorded `after-done` and released its claim.
emitted_zero, _, done_zero = finish(*success,
                                    settled=lambda ws, w: ws.reached("after-done") and not ws.claimed("task-demo"))
check("control: a successful run records after-done and releases its claim", done_zero,
      "the success path did not complete, so 'emits nothing' would be vacuous")
check("control: a successful run emits nothing and needs no failure", not emitted_zero)

pid1, pid2, emitted_r, published_r, body_r, settled_r = restart_witness(restart_ws, restart_first)
print(f"\n  restart witness: watcher pid {pid1} stopped, pid {pid2} started; task arrived after the restart")
print(f"    emitted to the live core: {emitted_r}")
print(f"    published by the restarted watcher: {published_r}")
print(f"    result body: {body_r.strip()[:120]!r}")
check("restart: the restarted watcher settles the task (handler ran, claim released)", settled_r)
check("restart: the restarted watcher does NOT hand the task to the core", not emitted_r)
check("restart: the restarted watcher publishes a terminal failure", published_r != [],
      "no result file, so the task is neither delivered nor failed")

print(("FAILED — " + ", ".join(FAILURES)) if FAILURES else "PASS — handler terminal rc outranks the probe-time disposition")
sys.exit(1 if FAILURES else 0)
