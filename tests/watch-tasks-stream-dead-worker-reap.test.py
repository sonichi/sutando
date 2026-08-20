#!/usr/bin/env python3
"""A dead handler worker must not hold its dispatch slot, and the reap must publish
a terminal failure unless an EXACT, non-empty result for that task already exists."""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []
FAILURE_TEXT = "could not safely process"


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


def names(d: Path | None) -> set[str]:
    if d is None or not d.is_dir():
        return set()
    return {p.name for p in d.iterdir() if p.is_file()}


class Harness:
    """One isolated watcher: own workspace, TMPDIR and session (cleanup() ends in
    `kill 0`). Only pids recorded here are killed — never a pattern match."""

    def __init__(self) -> None:
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
        h.handler, h.proc = tmp / "handler.sh", None
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
        # The watched dir is $1, NOT an env var — passing it as one would fall
        # through to the resolver and watch the REAL workspace.
        self.proc = subprocess.Popen(
            ["bash", "src/watch-tasks-stream.sh", str(self.ws / "tasks")],
            cwd=str(REPO), env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)

    def dispatch(self) -> Path | None:
        c = sorted(self.tmp.glob("sutando-task-dispatch.*"), key=lambda p: p.stat().st_mtime)
        return c[-1] if c else None

    def deliver(self, name: str) -> None:
        p = self.task(name)
        with self.feed.open("a") as fh:
            fh.write(str(p.resolve()) + "\n")

    def kill_workers(self) -> list[int]:
        pids = []
        d = self.dispatch()
        for r in (d / "workers").iterdir():
            try:
                pids.append(int(r.read_text().strip()))
            except (ValueError, OSError):
                pass
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        wait_for(lambda: all(not alive(p) for p in pids), 8.0)
        return pids

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


def scenario_slot_recovery() -> None:
    """Dead workers held both slots, so nothing dispatched again."""
    h = Harness()
    h.task("task-aaa.txt")
    h.task("task-bbb.txt")
    h.start()
    try:
        if not wait_for(lambda: len(names(h.dispatch() and h.dispatch() / "running")) >= 2):
            check("both worker slots filled by the startup sweep", False, str(h.dispatch()))
            return
        check("both worker slots filled by the startup sweep", True)
        check("worker pids recorded and killed", len(h.kill_workers()) >= 2)
        h.deliver("task-ccc.txt")
        got = wait_for(lambda: "task-ccc.txt" in names(h.dispatch() / "running"))
        d = h.dispatch()
        check("a task arriving after both workers died still gets dispatched", got,
              f"pending={names(d/'pending')} running={names(d/'running')}")
    finally:
        h.stop()


def scenario_reap_publishes_only_without_an_exact_result() -> None:
    """A prefix-colliding archive must not satisfy the id; a genuine archived
    result must still suppress the failure (so the fix is not "always publish")."""
    h = Harness()
    arch = h.ws / "results" / "archive"
    # `task-1234` must not satisfy `task-123` (prefix collision).
    (arch / "task-1234-999.txt").write_text("a different task's answer\n")
    # A genuine archived result for task-abc — the reap must stay silent here.
    (arch / "task-abc-999.txt").write_text("the real answer\n")
    h.task("task-123.txt")
    h.task("task-abc.txt")
    h.start()
    try:
        if not wait_for(lambda: len(names(h.dispatch() and h.dispatch() / "running")) >= 2):
            check("collision scenario: both slots filled", False)
            return
        h.kill_workers()
        h.deliver("task-zzz.txt")

        res = h.ws / "results"
        published = wait_for(lambda: (res / "task-123.txt").is_file()
                             and (res / "task-123.txt").stat().st_size > 0)
        check("prefix-colliding archive does NOT count as this task's result",
              published and FAILURE_TEXT in (res / "task-123.txt").read_text(),
              "no terminal failure published for task-123")
        # Negative control: the fix must not become "always publish".
        time.sleep(1.0)
        check("a genuine archived result still suppresses the terminal failure",
              not (res / "task-abc.txt").exists(),
              f"spurious failure written: {(res/'task-abc.txt').read_text()[:60] if (res/'task-abc.txt').exists() else ''}")
    finally:
        h.stop()


def scenario_placeholder_never_settles_the_task_as_success() -> None:
    """Empty and whitespace-only bodies are undeliverable, so neither may settle
    the task — the claim is held rather than released as if an answer existed."""
    h = Harness()
    h.task("task-456.txt")
    h.task("task-space.txt")
    h.start()
    try:
        if not wait_for(lambda: len(names(h.dispatch() and h.dispatch() / "running")) >= 2):
            check("placeholder scenario: both slots filled", False)
            return
        (h.ws / "results" / "task-456.txt").write_text("")
        (h.ws / "results" / "task-space.txt").write_text("   \n\t\n")
        claims = h.ws / "state" / "task-event-handler-claims"
        h.kill_workers()
        h.deliver("task-yyy.txt")
        wait_for(lambda: False, timeout=6.0,
                 nudge=lambda n: h.deliver(f"task-nudge-ph-{n}.txt"))
        for tid, label in (("task-456.txt", "a zero-byte"), ("task-space.txt", "a whitespace-only")):
            res = h.ws / "results" / tid
            check(f"{label} live result is not overwritten by the failure",
                  FAILURE_TEXT not in res.read_text(), f"body={res.read_text()[:40]!r}")
            check(f"{label} live result does NOT settle the claim as success",
                  (claims / tid).is_file(), "claim was released despite no answer")
    finally:
        h.stop()


def scenario_whitespace_archived_result_is_not_delivered() -> None:
    """An archived body that is whitespace-only never delivered an answer, so the
    reap must still publish rather than release the claim as success."""
    h = Harness()
    (h.ws / "results" / "archive" / "task-wsa-999.txt").write_text("  \n \n")
    h.task("task-wsa.txt")
    h.start()
    try:
        if not wait_for(lambda: len(names(h.dispatch() and h.dispatch() / "running")) >= 1):
            check("archived-whitespace scenario: slot filled", False)
            return
        h.kill_workers()
        h.deliver("task-www.txt")
        res = h.ws / "results" / "task-wsa.txt"
        check("a whitespace-only ARCHIVED result does NOT suppress the failure",
              wait_for(lambda: res.is_file() and FAILURE_TEXT in res.read_text(),
                       nudge=lambda n: h.deliver(f"task-nudge-wsa-{n}.txt")),
              f"exists={res.exists()}")
    finally:
        h.stop()


def scenario_unready_destination_is_left_untouched_and_unsettled() -> None:
    """A destination a provider may still own is neither read-then-moved nor
    replaced: no publish, no `.superseded.*`, and the claim stays held."""
    h = Harness()
    h.task("task-keep.txt")
    h.start()
    try:
        if not wait_for(lambda: len(names(h.dispatch() and h.dispatch() / "running")) >= 1):
            check("untouched scenario: slot filled", False)
            return
        placeholder = "   \n\t\n"
        res = h.ws / "results" / "task-keep.txt"
        res.write_text(placeholder)
        claims = h.ws / "state" / "task-event-handler-claims" / "task-keep.txt"
        held_before = claims.is_file()
        h.kill_workers()
        h.deliver("task-zzz.txt")
        # Give the reap the same room the publishing scenarios get, then assert
        # the invariant: it must NOT have acted on a destination it cannot own.
        wait_for(lambda: False, timeout=6.0,
                 nudge=lambda n: h.deliver(f"task-nudge-keep-{n}.txt"))
        check("an unready destination is left byte-for-byte untouched",
              res.read_text() == placeholder, f"body={res.read_text()[:60]!r}")
        check("no terminal failure is published over it",
              FAILURE_TEXT not in res.read_text())
        check("it is not moved aside to a hidden sibling either",
              not list((h.ws / "results").glob(".task-keep.txt.superseded.*")),
              f"found={[p.name for p in (h.ws / 'results').glob('.task-keep.txt.superseded.*')]}")
        check("the claim is left held, so the task is unsettled not falsely done",
              held_before and claims.is_file(),
              f"held_before={held_before} still_held={claims.is_file()}")
    finally:
        h.stop()


def scenario_answer_landing_during_the_reap_stays_deliverable() -> None:
    """The control qingyun-wu asked for: an answer arriving after the readiness
    check must remain at the DELIVERY path, not a hidden sibling."""
    h = Harness()
    h.task("task-late.txt")
    h.start()
    try:
        if not wait_for(lambda: len(names(h.dispatch() and h.dispatch() / "running")) >= 1):
            check("late-answer scenario: slot filled", False)
            return
        res = h.ws / "results" / "task-late.txt"
        res.write_text("  \n")
        h.kill_workers()
        h.deliver("task-qqq.txt")
        answer = "the answer the provider finished writing\n"
        tmp = h.ws / "results" / ".late-writer.tmp"
        tmp.write_text(answer)
        os.replace(tmp, res)
        wait_for(lambda: False, timeout=6.0,
                 nudge=lambda n: h.deliver(f"task-nudge-late-{n}.txt"))
        check("an answer that lands during the reap is still at the delivery path",
              res.read_text() == answer, f"delivery path holds {res.read_text()[:60]!r}")
        check("and was not relocated to a hidden sibling",
              not list((h.ws / "results").glob(".task-late.txt.superseded.*")))
    finally:
        h.stop()





def main() -> int:
    scenario_slot_recovery()
    scenario_reap_publishes_only_without_an_exact_result()
    scenario_placeholder_never_settles_the_task_as_success()
    scenario_whitespace_archived_result_is_not_delivered()
    scenario_unready_destination_is_left_untouched_and_unsettled()
    scenario_answer_landing_during_the_reap_stays_deliverable()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nPASS — dead workers free their slot; the reap publishes unless an exact result exists")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
